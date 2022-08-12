import sys
import math
import trimesh
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F

import raymarching
from .utils import custom_meshgrid
sys.path.append("..")  # Adds higher directory to python modules path.


def sample_pdf(bins, weights, n_samples, det=False):
    # This implementation is from NeRF
    # bins: [B, T], old_z_vals
    # weights: [B, T - 1], bin weights.
    # return: [B, n_samples], new_z_vals

    # Get pdf
    weights = weights + 1e-5  # prevent nans
    pdf = weights / torch.sum(weights, -1, keepdim=True)
    cdf = torch.cumsum(pdf, -1)
    cdf = torch.cat([torch.zeros_like(cdf[..., :1]), cdf], -1)
    # Take uniform samples
    if det:
        u = torch.linspace(0. + 0.5 / n_samples, 1. - 0.5 /
                           n_samples, steps=n_samples).to(weights.device)
        u = u.expand(list(cdf.shape[:-1]) + [n_samples])
    else:
        u = torch.rand(list(cdf.shape[:-1]) + [n_samples]).to(weights.device)

    # Invert CDF
    u = u.contiguous()
    inds = torch.searchsorted(cdf, u, right=True)
    below = torch.max(torch.zeros_like(inds - 1), inds - 1)
    above = torch.min((cdf.shape[-1] - 1) * torch.ones_like(inds), inds)
    inds_g = torch.stack([below, above], -1)  # (B, n_samples, 2)

    matched_shape = [inds_g.shape[0], inds_g.shape[1], cdf.shape[-1]]
    cdf_g = torch.gather(cdf.unsqueeze(1).expand(matched_shape), 2, inds_g)
    bins_g = torch.gather(bins.unsqueeze(1).expand(matched_shape), 2, inds_g)

    denom = (cdf_g[..., 1] - cdf_g[..., 0])
    denom = torch.where(denom < 1e-5, torch.ones_like(denom), denom)
    t = (u - cdf_g[..., 0]) / denom
    samples = bins_g[..., 0] + t * (bins_g[..., 1] - bins_g[..., 0])

    return samples


def plot_pointcloud(pc, color=None):
    # pc: [N, 3]
    # color: [N, 3/4]
    print('[visualize points]', pc.shape, pc.dtype, pc.min(0), pc.max(0))
    pc = trimesh.PointCloud(pc, color)
    # axis
    axes = trimesh.creation.axis(axis_length=4)
    # sphere
    sphere = trimesh.creation.icosphere(radius=1)
    trimesh.Scene([pc, axes, sphere]).show()


class NeRFRenderer(nn.Module):
    def __init__(self,
                 bound=1,
                 cuda_ray=False,
                 # scale up deltas (or sigmas), to make the density grid more sharp. larger value than 1 usually improves performance.
                 density_scale=1,
                 min_near=0.2,
                 density_thresh=0.01,
                 bg_radius=-1,
                 ):
        super().__init__()

        self.bound = bound
        self.cascade = 1 + math.ceil(math.log2(bound))
        self.time_size = 64
        self.grid_size = 128
        self.density_scale = density_scale
        self.min_near = min_near
        self.density_thresh = density_thresh
        self.bg_radius = bg_radius  # radius of the background sphere.

        # prepare aabb with a 6D tensor (xmin, ymin, zmin, xmax, ymax, zmax)
        # NOTE: aabb (can be rectangular) is only used to generate points, we still rely on bound (always cubic) to calculate density grid and hashing.
        aabb_train = torch.FloatTensor(
            [-bound, -bound, -bound, bound, bound, bound])
        aabb_infer = aabb_train.clone()
        self.register_buffer('aabb_train', aabb_train)
        self.register_buffer('aabb_infer', aabb_infer)

        # extra state for cuda raymarching
        self.cuda_ray = cuda_ray
        if cuda_ray:
            # density grid (with an extra time dimension)
            density_grid = torch.zeros(
                self.time_size, self.cascade, self.grid_size ** 3)  # [T, CAS, H * H * H]
            density_bitfield = torch.zeros(
                self.time_size, self.cascade * self.grid_size ** 3 // 8, dtype=torch.uint8)  # [T, CAS * H * H * H // 8]
            self.register_buffer('density_grid', density_grid)
            self.register_buffer('density_bitfield', density_bitfield)
            self.mean_density = 0
            self.iter_density = 0
            # time stamps for density grid
            times = ((torch.arange(self.time_size, dtype=torch.float32) +
                     0.5) / self.time_size).view(-1, 1, 1)  # [T, 1, 1]
            self.register_buffer('times', times)
            # step counter
            # 16 is hardcoded for averaging...
            step_counter = torch.zeros(16, 2, dtype=torch.int32)
            self.register_buffer('step_counter', step_counter)
            self.mean_count = 0
            self.local_step = 0

    def forward(self, x, d, t):
        raise NotImplementedError()

    # separated density and color query (can accelerate non-cuda-ray mode.)
    def density(self, x, t):
        raise NotImplementedError()

    def color(self, x, d, t, mask=None, **kwargs):
        raise NotImplementedError()

    def reset_extra_state(self):
        if not self.cuda_ray:
            return
        # density grid
        self.density_grid.zero_()
        self.mean_density = 0
        self.iter_density = 0
        # step counter
        self.step_counter.zero_()
        self.mean_count = 0
        self.local_step = 0

    # VERY SIMILAR SETUP TO NSFF
    # def run(self, rays_o, rays_d, time, num_steps=128, upsample_steps=128, bg_color=None, perturb=False, **kwargs):
    #     # rays_o, rays_d: [B, N, 3], assumes B == 1
    #     # time: [B, 1]
    #     # bg_color: [3] in range [0, 1]
    #     # return: image: [B, N, 3], depth: [B, N]

    #     prefix = rays_o.shape[:-1]
    #     rays_o = rays_o.contiguous().view(-1, 3)
    #     rays_d = rays_d.contiguous().view(-1, 3)

    #     N = rays_o.shape[0]  # N = B * N, in fact
    #     device = rays_o.device

    #     # choose aabb
    #     aabb = self.aabb_train if self.training else self.aabb_infer

    #     # sample steps
    #     nears, fars = raymarching.near_far_from_aabb(
    #         rays_o, rays_d, aabb, self.min_near)
    #     nears.unsqueeze_(-1)
    #     fars.unsqueeze_(-1)

    #     #print(f'nears = {nears.min().item()} ~ {nears.max().item()}, fars = {fars.min().item()} ~ {fars.max().item()}')

    #     z_vals = torch.linspace(0.0, 1.0, num_steps,
    #                             device=device).unsqueeze(0)  # [1, T]
    #     z_vals = z_vals.expand((N, num_steps))  # [N, T]
    #     z_vals = nears + (fars - nears) * z_vals  # [N, T], in [nears, fars]

    #     # perturb z_vals
    #     sample_dist = (fars - nears) / num_steps
    #     if perturb:
    #         z_vals = z_vals + \
    #             (torch.rand(z_vals.shape, device=device) - 0.5) * sample_dist
    #         # z_vals = z_vals.clamp(nears, fars) # avoid out of bounds xyzs.

    #     # generate xyzs
    #     # [N, 1, 3] * [N, T, 1] -> [N, T, 3]
    #     xyzs = rays_o.unsqueeze(-2) + \
    #         rays_d.unsqueeze(-2) * z_vals.unsqueeze(-1)
    #     xyzs = torch.min(torch.max(xyzs, aabb[:3]), aabb[3:])  # a manual clip.

    #     #plot_pointcloud(xyzs.reshape(-1, 3).detach().cpu().numpy())

    #     # query SDF and RGB
    #     density_outputs = self.density(xyzs.reshape(-1, 3), time)

    #     # sigmas = density_outputs['sigma'].view(N, num_steps) # [N, T]
    #     for k, v in density_outputs.items():
    #         density_outputs[k] = v.view(N, num_steps, -1)

    #     # upsample z_vals (nerf-like)
    #     if upsample_steps > 0:
    #         with torch.no_grad():

    #             deltas = z_vals[..., 1:] - z_vals[..., :-1]  # [N, T-1]
    #             deltas = torch.cat(
    #                 [deltas, sample_dist * torch.ones_like(deltas[..., :1])], dim=-1)

    #             alphas = 1 - torch.exp(-deltas * self.density_scale *
    #                                    density_outputs['sigma'].squeeze(-1))  # [N, T]
    #             alphas_shifted = torch.cat(
    #                 [torch.ones_like(alphas[..., :1]), 1 - alphas + 1e-15], dim=-1)  # [N, T+1]
    #             weights = alphas * \
    #                 torch.cumprod(alphas_shifted, dim=-1)[..., :-1]  # [N, T]

    #             # sample new z_vals
    #             z_vals_mid = (z_vals[..., :-1] + 0.5 *
    #                           deltas[..., :-1])  # [N, T-1]
    #             new_z_vals = sample_pdf(
    #                 z_vals_mid, weights[:, 1:-1], upsample_steps, det=not self.training).detach()  # [N, t]

    #             # [N, 1, 3] * [N, t, 1] -> [N, t, 3]
    #             new_xyzs = rays_o.unsqueeze(-2) + \
    #                 rays_d.unsqueeze(-2) * new_z_vals.unsqueeze(-1)
    #             # a manual clip.
    #             new_xyzs = torch.min(torch.max(new_xyzs, aabb[:3]), aabb[3:])

    #         # only forward new points to save computation
    #         new_density_outputs = self.density(new_xyzs.reshape(-1, 3), time)
    #         # new_sigmas = new_density_outputs['sigma'].view(N, upsample_steps) # [N, t]
    #         for k, v in new_density_outputs.items():
    #             new_density_outputs[k] = v.view(N, upsample_steps, -1)

    #         # re-order
    #         z_vals = torch.cat([z_vals, new_z_vals], dim=1)  # [N, T+t]
    #         z_vals, z_index = torch.sort(z_vals, dim=1)

    #         xyzs = torch.cat([xyzs, new_xyzs], dim=1)  # [N, T+t, 3]
    #         xyzs = torch.gather(
    #             xyzs, dim=1, index=z_index.unsqueeze(-1).expand_as(xyzs))

    #         for k in density_outputs:
    #             tmp_output = torch.cat(
    #                 [density_outputs[k], new_density_outputs[k]], dim=1)
    #             density_outputs[k] = torch.gather(
    #                 tmp_output, dim=1, index=z_index.unsqueeze(-1).expand_as(tmp_output))

    #     deltas = z_vals[..., 1:] - z_vals[..., :-1]  # [N, T+t-1]
    #     deltas = torch.cat(
    #         [deltas, sample_dist * torch.ones_like(deltas[..., :1])], dim=-1)
    #     alphas = 1 - torch.exp(-deltas * self.density_scale *
    #                            density_outputs['sigma'].squeeze(-1))  # [N, T+t]
    #     alphas_shifted = torch.cat(
    #         [torch.ones_like(alphas[..., :1]), 1 - alphas + 1e-15], dim=-1)  # [N, T+t+1]
    #     weights = alphas * \
    #         torch.cumprod(alphas_shifted, dim=-1)[..., :-1]  # [N, T+t]

    #     dirs = rays_d.view(-1, 1, 3).expand_as(xyzs)
    #     for k, v in density_outputs.items():
    #         density_outputs[k] = v.view(-1, v.shape[-1])

    #     mask = weights > 1e-4  # hard coded
    #     rgbs = self.color(xyzs.reshape(-1, 3), dirs.reshape(-1, 3),
    #                       mask=mask.reshape(-1), **density_outputs)
    #     rgbs = rgbs.view(N, -1, 3)  # [N, T+t, 3]

    #     #print(xyzs.shape, 'valid_rgb:', mask.sum().item())

    #     # calculate weight_sum (mask)
    #     weights_sum = weights.sum(dim=-1)  # [N]

    #     # calculate depth
    #     ori_z_vals = ((z_vals - nears) / (fars - nears)).clamp(0, 1)
    #     depth = torch.sum(weights * ori_z_vals, dim=-1)

    #     # calculate color
    #     image = torch.sum(weights.unsqueeze(-1) * rgbs,
    #                       dim=-2)  # [N, 3], in [0, 1]

    #     # mix background color
    #     if self.bg_radius > 0:
    #         # use the bg model to calculate bg_color
    #         sph = raymarching.sph_from_ray(
    #             rays_o, rays_d, self.bg_radius)  # [N, 2] in [-1, 1]
    #         bg_color = self.background(sph, rays_d.reshape(-1, 3))  # [N, 3]
    #     elif bg_color is None:
    #         bg_color = 1

    #     image = image + (1 - weights_sum).unsqueeze(-1) * bg_color

    #     image = image.view(*prefix, 3)
    #     depth = depth.view(*prefix)

    #     # tmp: reg loss in mip-nerf 360
    #     # z_vals_shifted = torch.cat([z_vals[..., 1:], sample_dist * torch.ones_like(z_vals[..., :1])], dim=-1)
    #     # mid_zs = (z_vals + z_vals_shifted) / 2 # [N, T]
    #     # loss_dist = (torch.abs(mid_zs.unsqueeze(1) - mid_zs.unsqueeze(2)) * (weights.unsqueeze(1) * weights.unsqueeze(2))).sum() + 1/3 * ((z_vals_shifted - z_vals_shifted) * (weights ** 2)).sum()

    #     return {
    #         'depth': depth,
    #         'image': image,
    #         'deform': density_outputs['deform'],
    #     }

    def clear_mem(self, args):
        for arg in args:
            arg = 0
        return

    def run_cuda(self, rays_o, rays_d, time, dt_gamma=0, bg_color=None, perturb=False, force_all_rays=False, max_steps=1024, **kwargs):
        # rays_o, rays_d: [B, N, 3], assumes B == 1
        # time: [B, 1], B == 1, so only one time is used.
        # return: image: [B, N, 3], depth: [B, N]

        # See what format the rays are in
        # print("rays_o.shape: {}".format(rays_o.shape))
        # print("rays_d.shape: {}".format(rays_d.shape))

        # TODO: If it's coordinates, find a way to split
        # the sets of xyz coordinates into `static` and `dynamic`

        rays_o = rays_o.contiguous().view(-1, 3)
        rays_d = rays_d.contiguous().view(-1, 3)

        N = rays_o.shape[0]  # N = B * N, in fact
        device = rays_o.device
        N_static = N//2
        N_dynamic = N//2

        rays_o_s = rays_o[:N_static, :]
        rays_o_d = rays_o[N_static:, :]
        rays_d_s = rays_d[:N_static, :]
        rays_d_d = rays_d[N_static:, :]
        prefix_s = (rays_o[:N_static, :].shape[0])
        prefix_d = (rays_o[N_static:, :].shape[0])

        # print("prefix_s: {}".format(prefix_s))
        # print("prefix_d: {}".format(prefix_d))
        # print("\nrays_o_s.shape: {}".format(rays_o_s.shape))
        # print("rays_o_d.shape: {}".format(rays_o_d.shape))
        # print("rays_d_s.shape: {}".format(rays_d_s.shape))
        # print("rays_d_d.shape: {}".format(rays_d_d.shape))

        # pre-calculate near far
        nears, fars = raymarching.near_far_from_aabb(
            rays_o, rays_d, self.aabb_train if self.training else self.aabb_infer, self.min_near)
        # nears_d, fars_d = raymarching.near_far_from_aabb(
        #     rays_o_d, rays_d_d, self.aabb_train if self.training else self.aabb_infer, self.min_near)

        nears_s, nears_d = nears[:N_static], nears[N_static:]
        fars_s, fars_d = fars[:N_static], fars[N_static:]

        print("nears_s.shape: {}".format(nears_s.shape))
        print("nears_d.shape: {}".format(nears_d.shape))
        print("fars_s.shape: {}".format(fars_s.shape))
        print("fars_d.shape: {}".format(fars_d.shape))
        print("self.bg_radius: {}".format(self.bg_radius))

        # mix background color
        if self.bg_radius > 0:
            # use the bg model to calculate bg_color
            sph = raymarching.sph_from_ray(
                rays_o_d, rays_d_d, self.bg_radius)  # [N, 2] in [-1, 1]
            bg_color = self.background(sph, rays_d)  # [N, 3]
        elif bg_color is None:
            bg_color = 1

        # determine the correct frame of density grid to use
        t = torch.floor(time[0][0] * self.time_size).clamp(min=0,
                                                           max=self.time_size - 1).long()

        results = {}

        if self.training:
            # setup counter
            counter = self.step_counter[self.local_step % 16]
            counter.zero_()  # set to 0
            self.local_step += 1

            xyzs_s, dirs_s, deltas_s, rays_s = raymarching.march_rays_train(
                rays_o_s, rays_d_s, self.bound, self.density_bitfield[t], self.cascade, self.grid_size, nears_s, fars_s, counter, self.mean_count, perturb, 128, force_all_rays, dt_gamma, max_steps)
            xyzs_d, dirs_d, deltas_d, rays_d = raymarching.march_rays_train(
                rays_o_d, rays_d_d, self.bound, self.density_bitfield[t], self.cascade, self.grid_size, nears_d, fars_d, counter, self.mean_count, perturb, 128, force_all_rays, dt_gamma, max_steps)

            # Amazing visualization (POINT-CLOUDS)
            # plot_pointcloud(xyzs.reshape(-1, 3).detach().cpu().numpy())

            # print("\n\nxyzs.mean: {}".format(xyzs.mean()))
            # print("rays_o.mean: {}".format(rays_o.mean()))
            # print("rays_d.mean: {}".format(rays_d.mean()))
            # print("time: {}".format(time))

            sigmas_s, rgbs_s = self(
                xyzs_s, dirs_s, time, svd="static")
            sigmas_s = self.density_scale * sigmas_s
            # print("xyzs.shape: {}".format(xyzs.shape))
            sigmas_d, rgbs_d, deform_d, blend, sf = self(
                xyzs_d, dirs_d, time, svd="dynamic")
            sigmas_d = self.density_scale * sigmas_d

            # We need the sceneflow from the dynamicNeRF.
            sceneflow_b = sf[..., :3]
            sceneflow_f = sf[..., 3:]

            results['deform_d'] = deform_d
            deform_d = 0
            torch.cuda.empty_cache()

            print("\n\n\nPHASE 1 COMPLETE!!!\n\n\n")

            print()
            print("sigmas_s.shape: {}".format(sigmas_s.shape))
            print("rgbs_s.shape: {}".format(rgbs_s.shape))
            print("sigmas_d.shape: {}".format(sigmas_d.shape))
            print("rgbs_d.shape: {}".format(rgbs_d.shape))
            print("xyzs_s.shape: {}".format(xyzs_s.shape))
            print("xyzs_d.shape: {}".format(xyzs_d.shape))
            print("blend.shape: {}".format(blend.shape))
            print("sf.shape: {}".format(sf.shape))

            print()
            print("sigmas_s.sum(): {}".format(sigmas_s.sum()))
            print("rgbs_s.sum(): {}".format(rgbs_s.sum()))
            print("sigmas_d.sum(): {}".format(sigmas_d.sum()))
            print("rgbs_d.sum(): {}".format(rgbs_d.sum()))
            print("xyzs_s.sum(): {}".format(xyzs_s.sum()))
            print("xyzs_d.sum(): {}".format(xyzs_d.sum()))
            print("blend.sum(): {}".format(blend.sum()))
            print("sf.sum(): {}".format(sf.sum()))

            # weights_full, depth_full, image_full_orig = raymarching.composite_rays_train_full(
            #     sigmas_s, rgbs_s, sigmas_d, rgbs_d, blend, deltas, rays)
            # image_full = image_full_orig + \
            #     (1 - weights_full).unsqueeze(-1) * bg_color
            # depth_full = torch.clamp(
            #     depth_full - nears, min=0) / (fars - nears)
            # image_full = image_full.view(*prefix, 3)
            # depth_full = depth_full.view(*prefix)

            # === STATIC ===
            # print("\nExecuting 1st pass...")
            weights_sum_s, depth_s, image_s_orig = raymarching.composite_rays_train(
                sigmas_s, rgbs_s, deltas_s, rays_s)
            print()
            print("weights_sum_s.shape: {}".format(weights_sum_s.shape))
            print("depth_s.shape: {}".format(depth_s.shape))
            print("image_s_orig.shape: {}".format(image_s_orig.shape))
            print("sigmas_s.shape: {}".format(sigmas_s.shape))
            print("rgbs_s.shape: {}".format(rgbs_s.shape))
            print("deltas_s.shape: {}".format(deltas_s.shape))
            print("rays_s.shape: {}".format(rays_s.shape))
            print("\image_s_orig: {}".format(image_s_orig))

            image_s = image_s_orig + \
                (1 - weights_sum_s).unsqueeze(-1) * bg_color
            depth_s = torch.clamp(
                depth_s - nears_s, min=0) / (fars_s - nears_s)
            image_s = image_s.view(prefix_s, 3)
            depth_s = depth_s.view(prefix_s)

            weights_sum_s, depth_s, image_s_orig = 0, 0, 0
            torch.cuda.empty_cache()
            # print("\image_s: {}".format(image_s))
            print("\n\n\nPHASE STATIC COMPLETE!!!\n\n\n")

            # === DYNAMIC ===
            # print("\nExecuting 2nd pass...")
            weights_sum_d, depth_d, image_d_orig = raymarching.composite_rays_train(
                sigmas_d, rgbs_d, deltas_d, rays_d)

            print()
            print("sigmas_d.shape: {}".format(sigmas_d.shape))
            print("rgbs_d.shape: {}".format(rgbs_d.shape))
            print("deltas_d.shape: {}".format(deltas_d.shape))
            print("rays_d.shape: {}".format(rays_d.shape))
            print("weights_sum_d.sum: {}".format(weights_sum_d.sum()))
            print("weights_sum_d.shape: {}".format(weights_sum_d.shape))
            print("depth_d.shape: {}".format(depth_d.shape))
            print("image_d_orig.shape: {}".format(image_d_orig.shape))

            print("\nweights_sum_d: {}".format(weights_sum_d))
            print("image_d_orig: {}".format(image_d_orig))

            image_d = image_d_orig + \
                (1 - weights_sum_d).unsqueeze(-1) * bg_color
            depth_d = torch.clamp(
                depth_d - nears_d, min=0) / (fars_d - nears_d)
            image_d = image_d.view(prefix_d, 3)
            depth_d = depth_d.view(prefix_d)

            # Cleanup
            results['sigmas_s'] = sigmas_s
            results['sigmas_d'] = sigmas_d
            results['rgbs_s'] = rgbs_s
            results['rgbs_d'] = rgbs_d
            rgbs_s, rgbs_d = 0, 0
            sigmas_s, sigmas_d = 0, 0
            results['depth_map_s'] = depth_s
            results['depth_map_d'] = depth_d
            print("\n\n\nPHASE DYNAMIC COMPLETE!!!\n\n\n")

            # TODO: We have everything that we need here
            # Required:
            #          - rgb_map_s
            #          - rgb_map_d
            #          - depth_map_s
            #          - depth_map_d
            #          - acc_map_s
            #          - acc_map_d
            #          - weights_s
            #          - weights_d
            #          - rgb_map_full
            #          - depth_map_full
            #          - acc_map_full
            #          - weights_full
            #          - dynamicness_map

            # dynamic prep -> frames 2 & 3
            pts_b = xyzs_d + sceneflow_b
            pts_f = xyzs_d + sceneflow_f
            results['sceneflow_f'] = sceneflow_f
            results['sceneflow_b'] = sceneflow_b
            sceneflow_b, sceneflow_f, sf = 0, 0, 0

            results['raw_pts'] = xyzs_d
            xyzs_s, xyzs_d = 0, 0
            torch.cuda.empty_cache()
            print("\n\n\nPHASE 4 COMPLETE!!!\n\n\n")

            # 3rd pass
            # print("\nExecuting 3rd pass...")
            sigmas_d_b, rgbs_d_b, _, _, sf_b = self(
                pts_b, dirs_d, time, svd="dynamic")
            sceneflow_b_b = sf_b[..., :3]
            sceneflow_b_f = sf_b[..., 3:]
            results['raw_pts_b'] = pts_b
            # print("raymarching.composite_rays_train 3rd pass...")
            weights_sum_d_b, _, image_d_b = raymarching.composite_rays_train(
                sigmas_d_b, rgbs_d_b, deltas_d, rays_d)
            results['sceneflow_b_f'] = sceneflow_b_f
            image_d_b = image_d_b + \
                (1 - weights_sum_d_b).unsqueeze(-1) * bg_color
            results['rgb_map_d_b'] = image_d_b
            results['acc_map_d_b'] = torch.abs(
                torch.sum(weights_sum_d_b - weights_sum_d, -1))

            # Remove from GPU memory
            sceneflow_b_f = 0
            image_d_b = 0
            # dynamic prep -> frames 4 & 5
            pts_b_b = pts_b + sceneflow_b_b
            sceneflow_b_b = 0
            results['raw_pts_b_b'] = pts_b_b
            sf_b, pts_b = 0, 0
            torch.cuda.empty_cache()

            # 4th pass
            # print("\nExecuting 4th pass...")
            # print("pts_f.shape: {}".format(pts_f.shape))
            sigmas_d_f, rgbs_d_f, _, _, sf_f = self(
                pts_f, dirs_d, time, svd="dynamic")
            sceneflow_f_b = sf_f[..., :3]
            sceneflow_f_f = sf_f[..., 3:]
            results['raw_pts_f'] = pts_f
            # print("raymarching.composite_rays_train 4th pass...")
            weights_sum_d_f, _, image_d_f = raymarching.composite_rays_train(
                sigmas_d_f, rgbs_d_f, deltas_d, rays_d)
            image_d_f = image_d_f + \
                (1 - weights_sum_d_f).unsqueeze(-1) * bg_color
            results['sceneflow_f_b'] = sceneflow_f_b
            results['rgb_map_d_f'] = image_d_f
            results['acc_map_d_f'] = torch.abs(
                torch.sum(weights_sum_d_f - weights_sum_d, -1))

            # Remove from GPU memory
            sceneflow_f_b = 0
            image_d_f = 0
            # dynamic prep -> frames 4 & 5
            pts_f_f = pts_f + sceneflow_f_f
            sceneflow_f_f = 0
            results['raw_pts_f_f'] = pts_f_f
            sf_f, pts_f,  = 0, 0
            torch.cuda.empty_cache()

            # 5th pass
            # print("\nExecuting 5th pass...")
            sigmas_d_b_b, rgbs_d_b_b, _, _, _ = self(
                pts_b_b, dirs_d, time, svd="dynamic")
            weights_sum_d_b_b, _, image_d_b_b = raymarching.composite_rays_train(
                sigmas_d_b_b, rgbs_d_b_b, deltas_d, rays_d)
            image_d_b_b = image_d_b_b + \
                (1 - weights_sum_d_b_b).unsqueeze(-1) * bg_color
            results['rgb_map_d_b_b'] = image_d_b_b

            # 6th pass
            # print("\nExecuting 6th pass...")
            sigmas_d_f_f, rgbs_d_f_f, _, _, _ = self(
                pts_f_f, dirs_d, time, svd="dynamic")
            weights_sum_d_f_f, _, image_d_f_f = raymarching.composite_rays_train(
                sigmas_d_f_f, rgbs_d_f_f, deltas_d, rays_d)
            image_d_f_f = image_d_f_f + \
                (1 - weights_sum_d_f_f).unsqueeze(-1) * bg_color
            results['rgb_map_d_f_f'] = image_d_f_f

            # All required outputs for calculating our losses
            results['image'] = image_d
            results['blending'] = blend
            # TODO: blend the static and dynamic models here
            results['rgb_map_full'] = image_d
            results['rgb_map_s'] = image_s
            results['rgb_map_d'] = image_d
            results['weights_s'] = weights_sum_s
            results['weights_d'] = weights_sum_d
            # results['dynamicness_map'] = torch.sum(weights_full * blending, -1)

        # [Inference]
        else:
            # print("\n\n\nRunning Inference for time t: {}\n\n\n".format(t))
            # allocate outputs
            # if use autocast, must init as half so it won't be autocasted and lose reference.
            #dtype = torch.half if torch.is_autocast_enabled() else torch.float32
            # output should always be float32! only network inference uses half.
            dtype = torch.float32

            weights_sum = torch.zeros(N, dtype=dtype, device=device)
            depth = torch.zeros(N, dtype=dtype, device=device)
            image = torch.zeros(N, 3, dtype=dtype, device=device)

            n_alive = N
            rays_alive = torch.arange(
                n_alive, dtype=torch.int32, device=device)  # [N]
            rays_t = nears_d.clone()  # [N]

            step = 0

            while step < max_steps:

                # count alive rays
                n_alive = rays_alive.shape[0]

                # exit loop
                if n_alive <= 0:
                    break

                # decide compact_steps
                n_step = max(min(N // n_alive, 8), 1)

                xyzs_s, dirs_s, deltas_s = raymarching.march_rays(n_alive, n_step, rays_alive, rays_t, rays_o_s, rays_d_s, self.bound,
                                                                  self.density_bitfield[t], self.cascade, self.grid_size, nears, fars, 128, perturb, dt_gamma, max_steps)
                xyzs_d, dirs_d, deltas_d = raymarching.march_rays(n_alive, n_step, rays_alive, rays_t, rays_o_d, rays_d_d, self.bound,
                                                                  self.density_bitfield[t], self.cascade, self.grid_size, nears, fars, 128, perturb, dt_gamma, max_steps)

                # print("\n\nxyzs.mean: {}".format(xyzs.mean()))
                # print("rays_o.mean: {}".format(rays_o.mean()))
                # print("rays_d.mean: {}".format(rays_d.mean()))
                # print("time: {}".format(time))

                sigmas_s, rgbs_s = self(
                    xyzs, dirs_s, time, svd="static")
                sigmas_d, rgbs_d, deform_d, blend, sf = self(
                    xyzs, dirs_d, time, svd="dynamic")

                sigmas_d = torch.unsqueeze(sigmas_d, 0)  # FIXME
                rgbs_d = torch.unsqueeze(rgbs_d, 0)  # FIXME

                # TODO: FIXME
                sigmas = self.density_scale * sigmas_d
                rgbs = rgbs_d
                raymarching.composite_rays(
                    n_alive, n_step, rays_alive, rays_t, sigmas, rgbs, deltas_d, weights_sum, depth, image)

                rays_alive = rays_alive[rays_alive >= 0]

                #print(f'step = {step}, n_step = {n_step}, n_alive = {n_alive}, xyzs: {xyzs.shape}')

                step += n_step

            image = image + (1 - weights_sum).unsqueeze(-1) * bg_color
            depth = torch.clamp(depth - nears_d, min=0) / (fars_d - nears_d)
            image = image.view(prefix_d, 3)
            depth = depth.view(prefix_d)

            # Only run during inference
            results['image'] = image
            results['depth'] = depth

        # FIXME: Assign props here
        # FIXME: Assign props here
        # FIXME: Assign props here
        results['deform'] = deform_d

        return results

    @torch.no_grad()
    def mark_untrained_grid(self, poses, intrinsic, S=64):
        # poses: [B, 4, 4]
        # intrinsic: [3, 3]

        if not self.cuda_ray:
            return

        if isinstance(poses, np.ndarray):
            poses = torch.from_numpy(poses)

        B = poses.shape[0]

        fx, fy, cx, cy = intrinsic

        X = torch.arange(self.grid_size, dtype=torch.int32,
                         device=self.density_bitfield.device).split(S)
        Y = torch.arange(self.grid_size, dtype=torch.int32,
                         device=self.density_bitfield.device).split(S)
        Z = torch.arange(self.grid_size, dtype=torch.int32,
                         device=self.density_bitfield.device).split(S)

        count = torch.zeros_like(self.density_grid[0])
        poses = poses.to(count.device)

        # 5-level loop, forgive me...

        for xs in X:
            for ys in Y:
                for zs in Z:

                    # construct points
                    xx, yy, zz = custom_meshgrid(xs, ys, zs)
                    # [N, 3], in [0, 128)
                    coords = torch.cat(
                        [xx.reshape(-1, 1), yy.reshape(-1, 1), zz.reshape(-1, 1)], dim=-1)
                    indices = raymarching.morton3D(coords).long()  # [N]
                    # [1, N, 3] in [-1, 1]
                    world_xyzs = (2 * coords.float() /
                                  (self.grid_size - 1) - 1).unsqueeze(0)

                    # cascading
                    for cas in range(self.cascade):
                        bound = min(2 ** cas, self.bound)
                        half_grid_size = bound / self.grid_size
                        # scale to current cascade's resolution
                        cas_world_xyzs = world_xyzs * (bound - half_grid_size)

                        # split batch to avoid OOM
                        head = 0
                        while head < B:
                            tail = min(head + S, B)

                            # world2cam transform (poses is c2w, so we need to transpose it. Another transpose is needed for batched matmul, so the final form is without transpose.)
                            cam_xyzs = cas_world_xyzs - \
                                poses[head:tail, :3, 3].unsqueeze(1)
                            # [S, N, 3]
                            cam_xyzs = cam_xyzs @ poses[head:tail, :3, :3]

                            # query if point is covered by any camera
                            mask_z = cam_xyzs[:, :, 2] > 0  # [S, N]
                            mask_x = torch.abs(
                                cam_xyzs[:, :, 0]) < cx / fx * cam_xyzs[:, :, 2] + half_grid_size * 2
                            mask_y = torch.abs(
                                cam_xyzs[:, :, 1]) < cy / fy * cam_xyzs[:, :, 2] + half_grid_size * 2
                            mask = (mask_z & mask_x & mask_y).sum(
                                0).reshape(-1)  # [N]

                            # update count
                            count[cas, indices] += mask
                            head += S

        # mark untrained grid as -1
        self.density_grid[count.unsqueeze(
            0).expand_as(self.density_grid) == 0] = -1

        #print(f'[mark untrained grid] {(count == 0).sum()} from {resolution ** 3 * self.cascade}')

    @torch.no_grad()
    def update_extra_state(self, decay=0.95, S=128):
        # call before each epoch to update extra states.

        if not self.cuda_ray:
            return

        # update density grid

        tmp_grid = - torch.ones_like(self.density_grid)

        # full update.
        if self.iter_density < 16:
            # if True:
            X = torch.arange(self.grid_size, dtype=torch.int32,
                             device=self.density_bitfield.device).split(S)
            Y = torch.arange(self.grid_size, dtype=torch.int32,
                             device=self.density_bitfield.device).split(S)
            Z = torch.arange(self.grid_size, dtype=torch.int32,
                             device=self.density_bitfield.device).split(S)

            for t, time in enumerate(self.times):
                for xs in X:
                    for ys in Y:
                        for zs in Z:

                            # construct points
                            xx, yy, zz = custom_meshgrid(xs, ys, zs)
                            # [N, 3], in [0, 128)
                            coords = torch.cat(
                                [xx.reshape(-1, 1), yy.reshape(-1, 1), zz.reshape(-1, 1)], dim=-1)
                            indices = raymarching.morton3D(
                                coords).long()  # [N]
                            xyzs = 2 * coords.float() / (self.grid_size - 1) - \
                                1  # [N, 3] in [-1, 1]

                            # cascading
                            for cas in range(self.cascade):
                                bound = min(2 ** cas, self.bound)
                                half_grid_size = bound / self.grid_size
                                half_time_size = 0.5 / self.time_size
                                # scale to current cascade's resolution
                                cas_xyzs = xyzs * (bound - half_grid_size)
                                # add noise in coord [-hgs, hgs]
                                cas_xyzs += (torch.rand_like(cas_xyzs)
                                             * 2 - 1) * half_grid_size
                                # add noise in time [-hts, hts]
                                time_perturb = time + \
                                    (torch.rand_like(time) * 2 - 1) * \
                                    half_time_size
                                # query density
                                sigmas = self.density(cas_xyzs, time_perturb)[
                                    'sigma'].reshape(-1).detach()
                                sigmas *= self.density_scale
                                # assign
                                tmp_grid[t, cas, indices] = sigmas

        # partial update (half the computation)
        # just update 100 times should be enough... too time consuming.
        elif self.iter_density < 100:
            N = self.grid_size ** 3 // 4  # T * C * H * H * H / 4
            for t, time in enumerate(self.times):
                for cas in range(self.cascade):
                    # random sample some positions
                    # [N, 3], in [0, 128)
                    coords = torch.randint(
                        0, self.grid_size, (N, 3), device=self.density_bitfield.device)
                    indices = raymarching.morton3D(coords).long()  # [N]
                    # random sample occupied positions
                    occ_indices = torch.nonzero(
                        self.density_grid[t, cas] > 0).squeeze(-1)  # [Nz]
                    rand_mask = torch.randint(0, occ_indices.shape[0], [
                                              N], dtype=torch.long, device=self.density_bitfield.device)
                    # [Nz] --> [N], allow for duplication
                    occ_indices = occ_indices[rand_mask]
                    occ_coords = raymarching.morton3D_invert(
                        occ_indices)  # [N, 3]
                    # concat
                    indices = torch.cat([indices, occ_indices], dim=0)
                    coords = torch.cat([coords, occ_coords], dim=0)
                    # same below
                    xyzs = 2 * coords.float() / (self.grid_size - 1) - \
                        1  # [N, 3] in [-1, 1]
                    bound = min(2 ** cas, self.bound)
                    half_grid_size = bound / self.grid_size
                    half_time_size = 0.5 / self.time_size
                    # scale to current cascade's resolution
                    cas_xyzs = xyzs * (bound - half_grid_size)
                    # add noise in [-hgs, hgs]
                    cas_xyzs += (torch.rand_like(cas_xyzs)
                                 * 2 - 1) * half_grid_size
                    # add noise in time [-hts, hts]
                    time_perturb = time + \
                        (torch.rand_like(time) * 2 - 1) * half_time_size
                    # query density
                    sigmas = self.density(cas_xyzs, time_perturb)[
                        'sigma'].reshape(-1).detach()
                    sigmas *= self.density_scale
                    # assign
                    tmp_grid[t, cas, indices] = sigmas

        # max-pool on tmp_grid for less aggressive culling [No significant improvement...]
        # invalid_mask = tmp_grid < 0
        # tmp_grid = F.max_pool3d(tmp_grid.view(self.cascade, 1, self.grid_size, self.grid_size, self.grid_size), kernel_size=3, stride=1, padding=1).view(self.cascade, -1)
        # tmp_grid[invalid_mask] = -1

        # ema update
        valid_mask = (self.density_grid >= 0) & (tmp_grid >= 0)
        self.density_grid[valid_mask] = torch.maximum(
            self.density_grid[valid_mask] * decay, tmp_grid[valid_mask])
        # -1 non-training regions are viewed as 0 density.
        self.mean_density = torch.mean(self.density_grid.clamp(min=0)).item()
        self.iter_density += 1

        # convert to bitfield
        density_thresh = min(self.mean_density, self.density_thresh)
        for t in range(self.time_size):
            raymarching.packbits(
                self.density_grid[t], density_thresh, self.density_bitfield[t])

        # update step counter
        total_step = min(16, self.local_step)
        if total_step > 0:
            self.mean_count = int(
                self.step_counter[:total_step, 0].sum().item() / total_step)
        self.local_step = 0

        #print(f'[density grid] min={self.density_grid.min().item():.4f}, max={self.density_grid.max().item():.4f}, mean={self.mean_density:.4f}, occ_rate={(self.density_grid > 0.01).sum() / (128**3 * self.cascade):.3f} | [step counter] mean={self.mean_count}')

    def render(self, rays_o, rays_d, time, staged=False, max_ray_batch=4096, **kwargs):
        # rays_o, rays_d: [B, N, 3], assumes B == 1
        # return: pred_rgb: [B, N, 3]

        if self.cuda_ray:
            _run = self.run_cuda
        else:
            _run = self.run

        B, N = rays_o.shape[:2]
        device = rays_o.device

        # never stage when cuda_ray
        if staged and not self.cuda_ray:
            depth = torch.empty((B, N), device=device)
            image = torch.empty((B, N, 3), device=device)

            for b in range(B):
                head = 0
                while head < N:
                    tail = min(head + max_ray_batch, N)
                    results_ = _run(
                        rays_o[b:b+1, head:tail], rays_d[b:b+1, head:tail], time[b:b+1], **kwargs)
                    depth[b:b+1, head:tail] = results_['depth']
                    image[b:b+1, head:tail] = results_['image']
                    head += max_ray_batch

            results = {}
            results['depth'] = depth
            results['image'] = image

        else:
            results = _run(rays_o, rays_d, time, **kwargs)

        return results