import os
import glob
import tqdm
import numpy as np

DATASET = "sa160"
DATASET_PATH = "/home/skhalid/Documents/datalake/sa160/"
# DATASET_PATH = "/home/skhalid/Desktop/sa160/"
RESULTS_FOLDER = "/home/skhalid/Documents/wildnerf/results/Ours/custom"
# all_clips = glob.glob(DATASET_PATH+"/*.mp4")
# all_clips = glob.glob(DATASET_PATH+"/*dissection_thermal*/*05_04*.mp4")  # DONE
# all_clips = glob.glob(DATASET_PATH+"/*abdominal_access*/*01_02*.mp4")
# all_clips = glob.glob(DATASET_PATH+"/*knot_pushing*/*11_07*.mp4")
# all_clips = glob.glob(DATASET_PATH+"/*knotting*/*12_01*.mp4")
# all_clips = glob.glob(DATASET_PATH+"/*knotting*/*12_02*.mp4")
# all_clips = glob.glob(DATASET_PATH+"/*cutting*/*03_05*.mp4")  # DONE - beaut
# all_clips = glob.glob(DATASET_PATH+"/*coagulation*/*07_03*.mp4")
# all_clips = glob.glob(DATASET_PATH+"/*blunt_dissection*/*04_04*.mp4") # DONE - beaut
# all_clips = glob.glob(DATASET_PATH+"/*blunt_dissection*/*04_09*.mp4")  # DONE (iffy)
# all_clips = glob.glob(DATASET_PATH+"/*cutting*/*03_07*.mp4")
# all_clips = glob.glob(DATASET_PATH+"/*cutting*/*03_09*.mp4")
all_clips = glob.glob(DATASET_PATH+"/*dissection_thermal*/*.mp4") + \
    glob.glob(DATASET_PATH+"/*knotting*/*.mp4") + \
    glob.glob(DATASET_PATH+"/*coagulation*/*.mp4") + \
    glob.glob(DATASET_PATH+"/*thread*/*.mp4") + \
    glob.glob(DATASET_PATH+"/*cutting*/*.mp4")
# all_clips = glob.glob(DATASET_PATH+"/*/*.mp4")
MAX_CASES = 100000

for clip in tqdm.tqdm(all_clips[:MAX_CASES]):
    # print("Extracting clip: {}".format(clip))
    case = clip.split("/")[-2] + "_" + clip.split("/")[-1].split(".")[0]

    BASE = "/home/skhalid/Documents/datalake/dnerf/custom/" + case + "/"
    DEPTH_FOLDER = BASE+"/disp_img_val/"
    ORIG_FOLDER = BASE+"/images/"
    DEST = "/home/skhalid/Desktop/" + DATASET + "/"

    new_dest_orig = DEST + case + "/orig"
    new_dest_recon = DEST + case + "/recon"
    new_dest_depth = DEST + case + "/depth"

    # ==> Create the new folder and add the results to it
    cmd = "mkdir -p " + BASE
    os.system(cmd)
    cmd = "mkdir -p " + DEPTH_FOLDER
    os.system(cmd)
    cmd = "mkdir -p " + ORIG_FOLDER
    os.system(cmd)
    cmd = "mkdir -p " + new_dest_orig
    os.system(cmd)
    cmd = "mkdir -p " + new_dest_recon
    os.system(cmd)
    cmd = "mkdir -p " + new_dest_depth
    os.system(cmd)

    # ==> Copy the clip over with the new name
    cmd = "cp -p " + clip + " " + BASE + "clippy1.mp4"
    os.system(cmd)

    # ==> Run colmap
    # - extraction and resuired files
    cmd = "./runner.sh --extract " + BASE + \
        "clippy1.mp4 clippy1.mp4 --dataset custom"
    os.system(cmd)

    # ==> Run the model
    cmd = "rm -rf custom/checkpoints/* && time ./runner.sh --run custom " + BASE
    os.system(cmd)

    # cmd = "mv " + ORIG_FOLDER + "/*.jpg " + new_dest_orig + "/"
    # os.system(cmd)

    cmd = "mv " + RESULTS_FOLDER + "/*.png " + new_dest_recon + "/"
    os.system(cmd)

    cmd = "mv " + DEPTH_FOLDER + "/*.png " + new_dest_depth + "/"
    os.system(cmd)

    cmd = "cp -pr " + BASE + " " + DEST + case + "/"
    os.system(cmd)

    print(cmd)
