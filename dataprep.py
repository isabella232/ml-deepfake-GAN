from torch.utils.data import DataLoader
from dataset.dataset_class import VidDataSet
from config import device
from dataset.video_extraction_conversion import generate_landmarks
import os
from datetime import datetime
import pickle as pkl
import random
from multiprocessing import Pool, cpu_count
import torch
import PIL
import cv2
import numpy as np
from face_alignment import FaceAlignment, LandmarksType
from network.utils import get_logger
import matplotlib

matplotlib.use('TkAgg')
# region DATASET PREPARATION
K = 8
logging = get_logger(__name__)


def preprocess_dataset(source, output, device='cpu', size=0, overwrite=False):
    """
    Starts the pre-processing of the VoxCeleb dataset used for the Talking Heads models. This process has the following
    steps:
    * Extract all frames of each video in the dataset. Frames of videos that are split in several files are joined
    together.
    * Select K+1 frames of each video that will be kept. K frames will be used to train the embedder network, while
    the other one will be used to train the generator network. The value of K can be configured in the config.py file.
    * Landmarks will be extracted for the face in each of the frames that are being kept.
    * The frames and the corresponding landmarks for each video will be saved in files (one for each video) in the
    output directory.
    We originally tried to process several videos simultaneously using multiprocessing, but this seems to actually
    slow down the process instead of speeding it up.
    :param source: Path to the raw VoxCeleb dataset.
    :param output: Path where the pre-processed videos will be stored.
    :param device: Device used to run the landmark extraction model.
    :param size: Size of the dataset to generate. If 0, the entire raw dataset will be processed, otherwise, as many
    videos will be processed as specified by this parameter.
    :param overwrite: f True, files that have already been processed will be overwritten, otherwise, they will be
    ignored and instead, different files will be loaded.
    """
    logging.info('===== DATASET PRE-PROCESSING =====')
    logging.info(f'Running on {device.type.upper()}.')
    logging.info(f'Saving K+1 random frames from each video (K = {K}).')
    fa = FaceAlignment(LandmarksType._2D, device=device.type)

    video_list = get_video_list(source, size, output, overwrite=overwrite)
    n_workers = int(cpu_count() / 2 - 1)
    logging.info(f'Processing {len(video_list)} videos with {n_workers} workers...')

    # start_time = datetime.now()
    # pool = Pool(processes=n_workers, initializer=init_pool, initargs=(fa, output))
    # pool.map(process_video_folder, video_list)

    init_pool(fa, output)
    counter = 1
    for v in video_list:
        start_time = datetime.now()
        process_video_folder(v)
        logging.info(f'{counter}/{len(video_list)}\t{datetime.now()-start_time}')
        counter += 1

    logging.info(f'All {len(video_list)} videos processed.')
    logging.info(
        f'Average processing time: {(datetime.now()-start_time)/len(video_list)}'
    )


def get_video_list(source, size, output, overwrite=True):
    """
    Extracts a list of paths to videos to pre-process during the current run.
    :param source: Path to the root directory of the dataset.
    :param size: Number of videos to return.
    :param output: Path where the pre-processed videos will be stored.
    :param overwrite: If True, files that have already been processed will be overwritten, otherwise, they will be
    ignored and instead, different files will be loaded.
    :return: List of paths to videos.
    """
    already_processed = []
    if not overwrite:
        already_processed = [
            os.path.splitext(video_id)[0]
            for root, dirs, files in os.walk(output)
            for video_id in files
        ]

    video_list = []
    counter = 0
    for root, dirs, files in os.walk(source):
        if (
            len(files) > 0
            and os.path.basename(os.path.normpath(root)) not in already_processed
        ):
            assert contains_only_videos(files) and len(dirs) == 0
            video_list.append((root, files))
            counter += 1
            if 0 < size <= counter:
                break

    return video_list


def init_pool(face_alignment, output):
    global _FA
    _FA = face_alignment
    global _OUT_DIR
    _OUT_DIR = output


def process_video_folder(video):
    """
    Extracts all frames from a video, selects K+1 random frames, and saves them along with their landmarks.
    :param video: 2-Tuple containing (1) the path to the folder where the video segments are located and (2) the file
    names of the video segments.
    """
    folder, files = video

    try:
        assert contains_only_videos(files)
        frames = np.concatenate(
            [extract_frames(os.path.join(folder, f)) for f in files]
        )
        frames = select_random_frames(frames)
        frame_mark = generate_landmarks(frames, fa=face_alignment)
        frame_mark = torch.from_numpy(np.array(frame_mark)).type(
            dtype=torch.float
        )  # K,2,224,224,3
        frame_mark = frame_mark.transpose(2, 4).to(device)
        # K,2,3,224,224
        save_video(
            frames=frame_mark,
            video_id=os.path.basename(os.path.normpath(folder)),
            path=_OUT_DIR,
        )
    except Exception as e:
        logging.error(
            f'Video {os.path.basename(os.path.normpath(folder))} could not be processed:\n{e}'
        )


def contains_only_videos(files, extension='.mp4'):
    """
    Checks whether the files provided all end with the specified video extension.
    :param files: List of file names.
    :param extension: Extension that all files should have.
    :return: True if all files end with the given extension.
    """
    return len([x for x in files if os.path.splitext(x)[1] != extension]) == 0


def extract_frames(video):
    """
    Extracts all frames of a video file. Frames are extracted in BGR format, but converted to RGB. The shape of the
    extracted frames is [height, width, channels]. Be aware that PyTorch models expect the channels to be the first
    dimension.
    :param video: Path to a video file.
    :return: NumPy array of frames in RGB.
    """
    cap = cv2.VideoCapture(video)

    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    frames = np.empty((n_frames, h, w, 3), np.dtype('uint8'))

    fn, ret = 0, True
    while fn < n_frames and ret:
        ret, img = cap.read()
        frames[fn] = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        fn += 1

    cap.release()
    return frames


def select_random_frames(frames):
    """
    Selects K+1 random frames from a list of frames.
    :param frames: Iterator of frames.
    :return: List of selected frames.
    """
    S = []
    while len(S) <= K:
        s = random.randint(0, len(frames) - 1)
        if s not in S:
            S.append(s)

    return [frames[s] for s in S]


def save_video(path, video_id, frame):
    """
    Generates the landmarks for the face in each provided frame and saves the frames and the landmarks as a pickled
    list of dictionaries with entries {'frame', 'landmarks'}.
    :param path: Path to the output folder where the file will be saved.
    :param video_id: Id of the video that was processed.
    :param frames: List of frames to save.
    :param face_alignment: Face Alignment model used to extract face landmarks.
    """
    if not os.path.isdir(path):
        os.makedirs(path)

    filename = f'{video_id}.vid'
    pkl.dump(frame, open(os.path.join(path, filename), 'wb'))
    logging.info(f'Saved file: {filename}')


preprocess_dataset(
    source='mp4', output='clean_dataset', device=device, size=10, overwrite=True
)
