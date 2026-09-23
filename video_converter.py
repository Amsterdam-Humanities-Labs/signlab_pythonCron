#!/usr/bin/env python3

"""
Requires FFmpeg installed on the system and accessible via PATH.
Uses video_api_client.py to get videos that need processing and update their status.
"""

import sys
import json
import logging
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import atexit
import signal
import os
import threading
import re
import time
import socket
import errno
import shutil

from sc_paths import sc_path, sc_root

# The install root (normally /web) is on sys.path for renderServer.video_api_client
sys.path.insert(0, sc_root())
from renderServer.video_api_client import VideoAPIClient

# The heartbeat client: the installed package, else the copy beside this script.
try:
    from signlab_client_monitor import ClientMonitor
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from python_client import ClientMonitor

# Log setup: the installed package, else the copy vendored beside this script
# (see README.md), else the stdlib.
try:
    from signlab_client_monitor import setup_rotating_logger
except ImportError:
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from python_client import setup_rotating_logger
    except ImportError:
        # Last resort, stdlib only: a missing file or dependency must never
        # stop this process from starting. Same files and rotation.
        import logging.handlers

        def setup_rotating_logger(path, name=None, level=logging.INFO,
                                  max_bytes=5 * 1024 * 1024, backup_count=5,
                                  to_stream=True, stream=None,
                                  fmt="[%(asctime)s] [%(levelname)s] %(message)s",
                                  datefmt=None):
            logger = logging.getLogger(name)
            logger.setLevel(level)
            handlers = [logging.handlers.RotatingFileHandler(
                path, maxBytes=max_bytes, backupCount=backup_count)]
            if to_stream:
                handlers.append(logging.StreamHandler(stream))
            for handler in handlers:
                handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
                logger.addHandler(handler)
            return logger

# Runtime data goes beside the script (production: /home/gomer/pythonCron),
# or under $PYTHONCRON_STATE_DIR, as scheduler_v2.py does.
STATE_DIR = os.environ.get('PYTHONCRON_STATE_DIR') or os.path.dirname(os.path.abspath(__file__))
LOG_DIR = Path(STATE_DIR) / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Configuration
BASE_DIR = Path(sc_path("media", "studioFiles"))
OUTPUT_DIR_RAW = Path(sc_path("media_raw"))
OUTPUT_DIR_POST = Path(sc_path("media_post"))
TYD_DIR_POST = Path(sc_path("media", "studioFilesMini", "tyd"))
UPLOADS_DIR = Path(sc_path("uploads"))
SERVICE_RECORDS_PATH = Path(sc_path("servicesRecords.json"))
LOCKFILE_PATH = Path("/tmp/process_files.lock")
SKIPPED_FILES_LOG = LOG_DIR / "skipped_files.log"
LOG_FILE = LOG_DIR / "conversion_errors.log"
SERVICE_NAME = "Convert Script"
API_URL = 'https://signcollect.nl/renderServer'

# Root logger: conversion_errors.log (5 MB x 5, as scheduler_v2.log) and stdout.
logger = setup_rotating_logger(str(LOG_FILE), stream=sys.stdout,
                               fmt='%(asctime)s - %(levelname)s - %(message)s')

# Initialize API client
api_client = VideoAPIClient(API_URL)

# Initialize Client Monitor
monitor = ClientMonitor(
    api_url="https://signcollect.nl/client_monitor_api/api.php",
    client_id="converter",
    client_name="Video Converter",
    description="Converts videos and generates thumbnails for the system",
    heartbeat_interval=43200  # 720 minutes (12 hours)
)


def acquire_lock(lockfile):
    """
    Acquire a lock by creating a lockfile atomically.
    Ensures that only one instance of the script runs at a time.

    Args:
        lockfile (Path): Path object representing the lockfile.

    Exits:
        If the lockfile already exists or cannot be created.
    """
    try:
        # Attempt to create the lockfile atomically
        fd = os.open(str(lockfile), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, 'w') as f:
            lock_info = {
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "timestamp": datetime.now().isoformat()
            }
            json.dump(lock_info, f)
        logger.info(f"Lockfile created at {lockfile} with info: {lock_info}")
    except OSError as e:
        if e.errno == errno.EEXIST:
            # Lockfile already exists
            logger.error("Lockfile exists. Another instance is running or the previous instance didn't exit cleanly.")
            print("Lockfile exists. Another instance is running or the previous instance didn't exit cleanly.")
            
            # Attempt to read the lockfile contents
            
            try:
                with lockfile.open('r') as f:
                    lock_info = json.load(f)
                    logger.info(f"Lockfile info: {lock_info}")
                    
                    #get timestamp from the lockfile
                    
                    lock_timestamp = datetime.fromisoformat(lock_info['timestamp'])
                    
                    current_timestamp = datetime.now()
                    
                    #we expect the lockfile to be updated every minute, so if difference is more than 30 minutes then we proceed anyway
                    if (current_timestamp - lock_timestamp).total_seconds() > 1800:
                        logger.warning("Lockfile is stale. Proceeding with the current instance.")
                    else:
                        logger.error("Lockfile is recent. Exiting.")
                        print(lock_timestamp)
                        sys.exit(1)
                    
                    
            except Exception as e:
                logger.error(f"Failed to read lockfile at {lockfile}: {e}")
                
        else:
            # Other OS-related errors
            logger.error(f"Failed to create lockfile at {lockfile}: {e}")
            print(f"Failed to create lockfile at {lockfile}: {e}")
            sys.exit(1)

    def remove_lockfile():
        """Remove the lockfile if it exists."""
        if lockfile.exists():
            try:
                lockfile.unlink()
                logger.info(f"Lockfile removed from {lockfile}")
            except Exception as e:
                logger.error(f"Failed to remove lockfile at {lockfile}: {e}")

    # Register the cleanup function to be called on normal program termination
    atexit.register(remove_lockfile)

    def handle_signal(signum, frame):
        """
        Handle termination signals to ensure lockfile is removed.

        Args:
            signum (int): Signal number.
            frame: Current stack frame (unused).
        """
        logger.info(f"Received termination signal: {signum}. Removing lockfile and exiting.")
        remove_lockfile()
        sys.exit(1)

    # Register signal handlers for graceful termination
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(sig, handle_signal)

    def update_lockfile_periodically():
        """
        Periodically update the lockfile with the latest timestamp to indicate the process is alive.
        This can help in identifying stale locks.
        """
        while True:
            try:
                lock_info = {
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "timestamp": datetime.now().isoformat()
                }
                with lockfile.open('w') as f:
                    json.dump(lock_info, f)
                logger.info(f"Lockfile updated at {lockfile} with info: {lock_info}")
            except Exception as e:
                logger.error(f"Failed to update lockfile at {lockfile}: {e}")
            time.sleep(60)  # Update every 60 seconds

    # Start the periodic updater in a daemon thread
    updater_thread = threading.Thread(target=update_lockfile_periodically, daemon=True)
    updater_thread.start()


def set_niceness(nice_value=19):
    """Set the niceness of the current process."""
    try:
        os.nice(nice_value)
        logger.info(f"Process niceness set to {nice_value}")
    except AttributeError:
        logger.warning("os.nice is not available on this operating system.")
    except PermissionError:
        logger.warning("Insufficient permissions to set niceness.")


def find_folders(base_dir):
    """Find directories starting with '2024' sorted from latest to earliest."""
    # Extract date from folder name and sort
    def extract_date(folder):
        try:
            return datetime.strptime(folder.name, "%Y-%m-%d")
        except ValueError:
            return datetime.min  # Assign the earliest possible date if format is incorrect

    folders = [folder for folder in base_dir.iterdir() if folder.is_dir() and folder.name.startswith('202')]
    sorted_folders = sorted(folders, key=extract_date, reverse=True)
    logger.info(f"Found {len(sorted_folders)} folders starting with '2024', sorted from latest to earliest.")
    
    #get date from today, convert to format 2024-10-24 then check if it exists in the sorted_folders and remove it
    today = datetime.now().strftime("%Y-%m-%d")
    # sorted_folders = [folder for folder in sorted_folders if folder.name != today]
    sorted_folders = [folder for folder in sorted_folders]

    
    return sorted_folders


def extract_date_from_filename(filename):
    """
    Extracts the date in YYYYMMDD format from filenames like M20241024_0001.MP4.
    Returns the date string if found, else None.
    """
    print(filename)
    match = re.match(r'^[MABLR](\d{8})_\d{4}\.(mp4|MP4|wav)$', filename)
    if match:
        return match.group(1)
    return None


def get_frame_count(video_path):
    """Get the frame count of the video using ffprobe via subprocess."""
    try:
        result = subprocess.run([
            'ffprobe', '-v', 'error',
            '-select_streams', 'v:0',
            '-count_frames',
            '-show_entries', 'stream=nb_read_frames',
            '-of', 'default=nokey=1:noprint_wrappers=1',
            str(video_path)
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        frame_count_str = result.stdout.strip()
        logger.info(f"Frame count of the video {video_path}: {frame_count_str} frames")
        
        if not frame_count_str or frame_count_str == "N/A":
            logger.warning(f"Unable to determine frame count of the video: {video_path}. Setting default frame count to 5.")
            return 5
        else:
            return int(frame_count_str)
    except subprocess.CalledProcessError as e:
        logger.error(f"ffprobe failed for {video_path}: {e}")
        return 5  # Default frame count if ffprobe fails


def generate_thumbnail_ffmpeg(video_path):
    """
    Generate a thumbnail for the given video at the midpoint frame.
    Returns the thumbnail path if successful, None otherwise.
    """
    thumbnail_path = video_path.with_suffix('.jpg')
    
    if (thumbnail_path.exists()):
        logger.info(f"Thumbnail already exists for {video_path}. Skipping.")
        return thumbnail_path
    
    logger.info(f"Generating thumbnail for {video_path}")
    

    #if video is larger than 300MB, get frame from count 60 and skip ffprobe
    if video_path.stat().st_size > 300 * 1024 * 1024:  # 300 MB
        logger.info(f"Video {video_path} is larger than 300 MB. Using frame count 60 for thumbnail generation.")
        timestamp = 60  # Use 60 seconds as a fixed timestamp for large videos
    else:
        frame_count = get_frame_count(video_path)
        midpoint_frame = frame_count // 2
        
        try:
            # Calculate the timestamp for the midpoint frame
            result = subprocess.run([
                'ffprobe', '-v', 'error',
                '-select_streams', 'v:0',
                '-show_entries', 'stream=avg_frame_rate,duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                str(video_path)
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            
            lines = result.stdout.strip().split('\n')
            if len(lines) < 2:
                logger.warning(f"Insufficient ffprobe output for {video_path}. Setting timestamp to 0.")
                timestamp = 0
            else:
                avg_frame_rate, duration = lines[:2]
                nums = avg_frame_rate.split('/')
                if len(nums) == 2 and int(nums[1]) != 0:
                    frame_rate = float(nums[0]) / float(nums[1])
                    timestamp = midpoint_frame / frame_rate if frame_rate else 0
                else:
                    timestamp = 0
            
            logger.info(f"Midpoint timestamp for {video_path}: {timestamp} seconds")
        
        except subprocess.CalledProcessError as e:
            logger.error(f"ffprobe failed for {video_path}: {e}")
            return None
            
        # Construct FFmpeg command for thumbnail generation
        ffmpeg_cmd = [
            'ffmpeg',
            '-y',
            '-loglevel', 'info',
            '-ss', str(timestamp),
            '-i', str(video_path),
            '-frames:v', '1',
            str(thumbnail_path)
        ]
        
        # Execute FFmpeg command
        result = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        if result.returncode == 0:
            logger.info(f"Thumbnail generated at {thumbnail_path}")
            return thumbnail_path
        else:
            logger.error(f"FFmpeg error generating thumbnail for {video_path}: {result.stderr}")
            return None




def process_directory_for_thumbnails(directory):
    #convert directory from str to Path
    directory = Path(directory)
    
    
    """Process video files in the given directory to generate thumbnails."""
    logger.info(f"Processing directory for thumbnails: {directory}")
    if not directory.is_dir():
        logger.error(f"Directory not found: {directory}")
        return
    
    video_extensions = ['.mp4', '.MP4', '.mov', '.MOV', '.webm', '.WEBM']
    
    # Filter videos with both extension and naming pattern checks
    videos = []
    errors = 0
    for video in directory.iterdir():
        if video.suffix in video_extensions and video.is_file():
            # Check if filename matches expected pattern
            match = re.match(r'^([A-Z])(\d{8})_(\d+)\.(mp4|MP4|mov|MOV|webm|WEBM)$', video.name, re.IGNORECASE)
            if match:
                videos.append(video)
            else:
                logger.warning(f"Skipping {video} as it does not match expected naming pattern.")
                errors += 1
    
    logger.info(f"Found {len(videos)} videos in {directory} for thumbnail generation. Skipped {errors} files due to naming pattern.")
    
    with ThreadPoolExecutor(max_workers=1) as executor:  # Adjusted workers for better performance
        futures = [executor.submit(generate_thumbnail_ffmpeg, video) for video in videos]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                logger.error(f"Error generating thumbnail: {e}")


def update_service_records(service_name, service_records_path):
    """Update the service records JSON file with the current date."""
    current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        if service_records_path.exists():
            with service_records_path.open('r') as f:
                data = json.load(f)
        else:
            data = []
        
        # Check if service entry exists
        service_entry = next((item for item in data if item.get('service') == service_name), None)
        if service_entry:
            service_entry['date'] = current_date
            logger.info(f"Updated service entry for {service_name} with date {current_date}")
        else:
            data.append({"service": service_name, "date": current_date})
            logger.info(f"Added new service entry for {service_name} with date {current_date}")
        
        # Write back to the JSON file
        with service_records_path.open('w') as f:
            json.dump(data, f, indent=4)
        logger.info(f"Service records updated at {service_records_path}")
    except Exception as e:
        logger.error(f"Failed to update service records: {e}")


def process_unconverted_videos():
    """Process videos that have not been converted yet by scanning through date directories."""
    logger.info("Scanning date directories for videos that need conversion...")
    
    try:
        # Get date folders from the past three months
        date_folders = find_folders(BASE_DIR)
        
        # Filter for only the past three months
        current_date = datetime.now()
        three_months_ago = current_date - timedelta(days=90)
        
        filtered_folders = []
        for folder in date_folders:
            try:
                folder_date = datetime.strptime(folder.name, "%Y-%m-%d")
                if folder_date >= three_months_ago:
                    filtered_folders.append(folder)
            except ValueError:
                continue

        
        logger.info(f"Processing {len(filtered_folders)} date folders from the past three months")
        
        # Track statistics
        conversions_done = 0
        files_copied = 0
        errors = 0
        
        for date_folder in filtered_folders:
            raw_folder = date_folder / "raw"
            converted_folder = date_folder / "converted"
            #added post folder as we have some post videos missing
            post_folder = date_folder / "post"
            
            if not raw_folder.exists() or not raw_folder.is_dir():
                continue
                
            logger.info(f"Processing raw folder: {raw_folder}")
            
            # Ensure converted folder exists
            converted_folder.mkdir(parents=True, exist_ok=True)
            
            # Find all video files in raw folder
            video_extensions = ['.mp4', '.MP4', '.mov', '.MOV']
            video_files = [f for f in raw_folder.iterdir() if f.is_file() and f.suffix in video_extensions]
            
            for raw_video in video_files:

                #check if the raw_video name follows expected pattern
                match = re.match(r'^([A-Z])(\d{8})_(\d+)\.mp4$', raw_video.name, re.IGNORECASE)

                if not match:
                    logger.warning(f"Skipping {raw_video} as it does not match expected naming pattern.")
                    errors += 1
                    continue

                # Check if converted version already exists
                converted_video = converted_folder / raw_video.name
                
                #skip the file in raw when the size is greather than 1GB
                if raw_video.stat().st_size > 3 * 1024 * 1024 * 1024:  # 1 GB
                    logger.warning(f"Skipping {raw_video} as it exceeds 1 GB.")
                    errors += 1
                    continue

                if not converted_video.exists():
                    logger.info(f"Converting {raw_video} to {converted_video}")
                    
                    # Construct FFmpeg command
                    ffmpeg_cmd = [
                        'ffmpeg',
                        '-loglevel', 'info',
                        '-nostdin',
                        '-i', str(raw_video),
                        '-c:v', 'libx264',
                        '-c:a', 'aac',
                        '-pix_fmt', 'yuv420p',
                        '-profile:v', 'baseline',
                        '-level', '3',
                        str(converted_video),
                        '-n'
                    ]
                    
                    try:
                        # Execute FFmpeg conversion
                        result = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                        
                        if result.returncode == 0:
                            logger.info(f"Successfully converted {raw_video} to {converted_video}")
                            conversions_done += 1
                        else:
                            logger.error(f"FFmpeg error converting {raw_video}: {result.stderr}")
                            errors += 1
                            continue
                    except Exception as e:
                        logger.error(f"Exception during conversion of {raw_video}: {e}")
                        errors += 1
                        continue
                
                # Check if converted file exists in studioFilesMini/raw and copy if needed
                # if converted_video.exists():
                #     mini_raw_file = OUTPUT_DIR_RAW / converted_video.name
                    
                #     if not mini_raw_file.exists():
                #         try:
                #             # Ensure the mini raw directory exists
                #             OUTPUT_DIR_RAW.mkdir(parents=True, exist_ok=True)
                            
                #             # Use rclone copy instead of shutil.copy2 for cloud storage
                #             rclone_cmd = [
                #                 'rclone', 'copy',
                #                 str(converted_video),
                #                 str(OUTPUT_DIR_RAW),
                #                 '--progress'
                #             ]
                            
                #             result = subprocess.run(rclone_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                            
                #             if result.returncode == 0:
                #                 files_copied += 1
                #                 logger.info(f"Copied {converted_video} to {mini_raw_file}")
                #             else:
                #                 errors += 1
                #                 logger.error(f"rclone copy failed for {converted_video}: {result.stderr}")
                #         except Exception as e:
                #             errors += 1
                #             logger.error(f"Failed to copy {converted_video} to {mini_raw_file}: {e}")

                #for post video files
                if post_folder.exists() and post_folder.is_dir():
                    post_video = post_folder / raw_video.name
                    
                    if post_video.exists():
                        mini_post_file = OUTPUT_DIR_POST / post_video.name
                        
                        if not mini_post_file.exists():
                            try:
                                # Ensure the mini post directory exists
                                OUTPUT_DIR_POST.mkdir(parents=True, exist_ok=True)
                                
                                # Use rclone copy instead of shutil.copy2 for cloud storage
                                rclone_cmd = [
                                    'rclone', 'copy',
                                    str(post_video),
                                    str(OUTPUT_DIR_POST),
                                    '--progress'
                                ]
                                
                                result = subprocess.run(rclone_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                                
                                if result.returncode == 0:
                                    files_copied += 1
                                    logger.info(f"Copied {post_video} to {mini_post_file}")
                                else:
                                    errors += 1
                                    logger.error(f"rclone copy failed for {post_video}: {result.stderr}")
                            except Exception as e:
                                errors += 1
                                logger.error(f"Failed to copy {post_video} to {mini_post_file}: {e}")
        
        # Log summary statistics
        logger.info(f"Conversion process complete: {conversions_done} conversions done, {files_copied} files copied to mini directory, {errors} errors")
    
    except Exception as e:
        logger.error(f"Error processing unconverted videos: {e}")


def process_video_conversion(input_file, type):
    """
    Process a video file for conversion.
    Returns the output file path if successful, None otherwise.
    """
    # Determine if this is for raw or post directory based on file path
    file_str = str(input_file)
    
    # Extract stem name and remove _h264 if present
    name = input_file.stem
    if '_h264' in name:
        name = name.replace('_h264', '')
    
    if type == 'raw':
        output_dir = OUTPUT_DIR_RAW
    if type == 'post':
        output_dir = OUTPUT_DIR_POST
    # Determine output directory and path

    output_file = output_dir / f"{name}.mp4"
    

    
    # Check if output file already exists
    if output_file.exists():
        logger.info(f"Output file already exists: {output_file}. Skipping conversion.")
        return output_file
    
    # Ensure the output directory exists
    output_dir.mkdir(parents=True, exist_ok=True)
    
    logger.info(f"Converting {input_file} to {output_file}")
    
    # Construct FFmpeg command
    ffmpeg_cmd = [
        'ffmpeg',
        '-loglevel', 'info',
        '-nostdin',
        '-i', str(input_file),
        '-c:v', 'libx264',
        '-c:a', 'aac',
        '-pix_fmt', 'yuv420p',
        '-profile:v', 'baseline',
        '-level', '3',
        str(output_file),
        '-n'
    ]
    
    try:
        # Execute FFmpeg command
        result = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        if result.returncode == 0:
            logger.info(f"Successfully converted {input_file} to {output_file}")
            return output_file
        else:
            logger.error(f"FFmpeg error converting {input_file} to {output_file}: {result.stderr}")
            with open(SKIPPED_FILES_LOG, 'a') as f:
                f.write(f"{input_file}\n")
            return None
    except Exception as e:
        logger.error(f"Exception occurred while converting {input_file} to {output_file}: {e}")
        with open(SKIPPED_FILES_LOG, 'a') as f:
            f.write(f"{input_file}\n")
        return None


def process_thumbnail_null_videos():
    """Process videos that don't have thumbnails yet."""
    logger.info("Fetching videos that need thumbnails...")
    
    try:
        # Get videos that need thumbnails from the API
        no_thumbnail_videos = api_client.get_videos_thumbnail_null()
        logger.info(f"Found {len(no_thumbnail_videos)} videos that need thumbnails")
        
        # Process each video
        for video in no_thumbnail_videos:
            # Check if we have the required file information
            if not video.get('m_file'):
                logger.warning(f"Video ID {video.get('id')} missing m_file. Skipping.")
                continue
                
            # Get the m_file value and create path to post directory MP4
            m_file = video['m_file']
            video_file = BASE_DIR / m_file
            thumbnail_generated = False

            # Add the thumbnail based on OUTPUT_DIR_POST
            date_str = extract_date_from_filename(m_file)
            if date_str:
                date_obj = datetime.strptime(date_str, "%Y%m%d")
                date_str = date_obj.strftime("%Y-%m-%d")
                post_video_file = OUTPUT_DIR_POST / m_file
                post_video_file = post_video_file.with_suffix('.mp4')
                
                print(post_video_file)
                if post_video_file.exists():
                    logger.info(f"Processing thumbnail for POST dir: {post_video_file}")
                    post_thumbnail_path = generate_thumbnail_ffmpeg(post_video_file)
                    if post_thumbnail_path and post_thumbnail_path.exists():
                            result = api_client.update_thumbnail(m_file)

                
                alt_post_file = OUTPUT_DIR_POST / f"{m_file.replace('.wav', '.mp4')}"
                if alt_post_file.exists():
                    logger.info(f"Processing thumbnail for POST alt: {alt_post_file}")
                    alt_post_thumbnail_path = generate_thumbnail_ffmpeg(alt_post_file)
                    if alt_post_thumbnail_path and alt_post_thumbnail_path.exists():
                            result = api_client.update_thumbnail(m_file)
            
            # Add the thumbnail based on OUTPUT_DIR_RAW
            if date_str:
                raw_video_file = OUTPUT_DIR_RAW / m_file
                raw_video_file = raw_video_file.with_suffix('.MP4')
                if raw_video_file.exists():
                    logger.info(f"Processing thumbnail for RAW dir: {raw_video_file}")
                    raw_thumbnail_path = generate_thumbnail_ffmpeg(raw_video_file)
                    if raw_thumbnail_path and raw_thumbnail_path.exists():
                        thumbnail_generated = True
                
                alt_raw_file = OUTPUT_DIR_RAW / f"{m_file.replace('.wav', '.MP4')}"
                if alt_raw_file.exists():
                    logger.info(f"Processing thumbnail for RAW alt: {alt_raw_file}")
                    alt_raw_thumbnail_path = generate_thumbnail_ffmpeg(alt_raw_file)
                    if alt_raw_thumbnail_path and alt_raw_thumbnail_path.exists():
                        thumbnail_generated = True
            
            # Add the thumbnail based on TYD_DIR_POST
            if date_str:
                tyd_video_file = TYD_DIR_POST / m_file
                tyd_video_file = tyd_video_file.with_suffix('.mp4')
                if tyd_video_file.exists():
                    logger.info(f"Processing thumbnail for TYD dir: {tyd_video_file}")
                    tyd_thumbnail_path = generate_thumbnail_ffmpeg(tyd_video_file)
                    if tyd_thumbnail_path and tyd_thumbnail_path.exists():
                        result = api_client.update_tyd_thumbnail(m_file, "m_file")
                
                alt_tyd_file = TYD_DIR_POST / f"{m_file.replace('.wav', '.MP4')}"
                if alt_tyd_file.exists():
                    logger.info(f"Processing thumbnail for TYD alt: {alt_tyd_file}")
                    alt_tyd_thumbnail_path = generate_thumbnail_ffmpeg(alt_tyd_file)
                    if alt_tyd_thumbnail_path and alt_tyd_thumbnail_path.exists():
                        result = api_client.update_tyd_thumbnail(m_file, "m_file")
            
    
    except Exception as e:
        logger.error(f"Error processing videos without thumbnails: {e}")

def copy_cloud_to_local_videos():
    """
    Copy videos from cloud storage (BASE_DIR) to local directories (OUTPUT_DIR_RAW and OUTPUT_DIR_POST)
    if they don't already exist locally. Only processes folders from the past three months.
    """
    logger.info("Starting to copy cloud videos to local storage (past three months only)...")
    
    # Find date folders sorted from latest to earliest
    date_folders = find_folders(BASE_DIR)
    
    # Filter for only the past three months
    current_date = datetime.now()
    # Calculate three months ago more safely
    three_months_ago = current_date - timedelta(days=90)  # Approximately 3 months
    
    filtered_folders = []
    for folder in date_folders:
        try:
            folder_date = datetime.strptime(folder.name, "%Y-%m-%d")
            if folder_date >= three_months_ago:
                filtered_folders.append(folder)
        except ValueError:
            # Skip folders that don't match the expected date format
            continue
    
    logger.info(f"Found {len(filtered_folders)} folders from the past three months out of {len(date_folders)} total folders")
    
    # Track statistics
    files_copied = 0
    files_skipped = 0
    errors = 0
    
    for date_folder in filtered_folders:
        # Process converted folder (for raw)
        # converted_folder = date_folder / "converted"
        # if converted_folder.exists() and converted_folder.is_dir():
        #     logger.info(f"Processing converted folder: {converted_folder}")
            
        #     # Find all MP4 files in the converted folder
        #     video_files = [f for f in converted_folder.iterdir() if f.is_file() and f.suffix.lower() in ('.mp4', '.mp4')]
            
        #     for video_file in video_files:
        #         name = video_file.stem
        #         local_file = OUTPUT_DIR_RAW / f"{name}.mp4"
        #         local_file_caps = OUTPUT_DIR_RAW / f"{name}.MP4"

        #         #check if the file is not above 100 MB
        #         if video_file.stat().st_size > 100 * 1024 * 1024:
        #             logger.warning(f"File {video_file} is larger than 100 MB. Skipping.")
        #             files_skipped += 1
        #             continue
                
        #         match = re.match(r'^([A-Z])(\d{8})_(\d+)\.mp4$', name, re.IGNORECASE)
        #         if not match:
        #             logger.warning(f"Skipping {video_file} as it does not match expected naming pattern.")
        #             errors += 1
        #             continue


        #         # Check if file already exists locally
        #         if not local_file.exists() and not local_file_caps.exists():
        #             try:
        #                 # Ensure the output directory exists
        #                 OUTPUT_DIR_RAW.mkdir(parents=True, exist_ok=True)
                        
        #                 # Use rclone copy instead of shutil.copy2 for cloud storage
        #                 rclone_cmd = [
        #                     'rclone', 'copy',
        #                     str(video_file),
        #                     str(OUTPUT_DIR_RAW),
        #                     '--progress'
        #                 ]
                        
        #                 result = subprocess.run(rclone_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                        
        #                 if result.returncode == 0:
        #                     files_copied += 1
        #                     logger.info(f"Copied {video_file} to {local_file}")
        #                 else:
        #                     errors += 1
        #                     logger.error(f"rclone copy failed for {video_file}: {result.stderr}")
        #             except Exception as e:
        #                 errors += 1
        #                 logger.error(f"Failed to copy {video_file} to {local_file}: {e}")
        #         else:
        #             files_skipped += 1
        #             logger.debug(f"File already exists locally: {local_file}. Skipping.")
        
        # Process post folder
        post_folder = date_folder / "post"
        if post_folder.exists() and post_folder.is_dir():
            logger.info(f"Processing post folder: {post_folder}")
            
            # Find only MP4 files with _h264 in the name in the post folder
            video_files = [f for f in post_folder.iterdir() 
                          if f.is_file() and f.suffix.lower() in ('.mp4', '.mp4') and '_h264' in f.stem]
            for video_file in video_files:
                name = video_file.stem
                # Remove _h264 suffix if present for post files
                if '_h264' in name:
                    name = name.replace('_h264', '')
                
                local_file = OUTPUT_DIR_POST / f"{name}.mp4"
                local_file_caps = OUTPUT_DIR_POST / f"{name}.MP4"

                #check if the file is not above 100 MB
                if video_file.stat().st_size > 100 * 1024 * 1024:
                    logger.warning(f"File {video_file} is larger than 100 MB. Skipping.")
                    files_skipped += 1
                    continue
                
                # Extract the base name without _h264 for pattern matching
                base_name = video_file.stem.replace('_h264', '')
                match = re.match(r'^([A-Z])(\d{8})_(\d+)$', base_name, re.IGNORECASE)
                if not match:
                    logger.warning(f"Skipping {video_file} as it does not match expected naming pattern.")
                    errors += 1
                    continue

                # Check if file already exists locally
                if not local_file.exists() and not local_file_caps.exists():
                    try:
                        # Ensure the output directory exists
                        OUTPUT_DIR_POST.mkdir(parents=True, exist_ok=True)
                        
                        # Use rclone copy instead of shutil.copy2 for cloud storage
                        rclone_cmd = [
                            'rclone', 'copyto',
                            str(video_file),
                            str(local_file),
                            '--progress'
                        ]
                        
                        result = subprocess.run(rclone_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                        
                        if result.returncode == 0:
                            files_copied += 1
                            logger.info(f"Copied {video_file} to {local_file}")
                        else:
                            errors += 1
                            logger.error(f"rclone copy failed for {video_file}: {result.stderr}")
                    except Exception as e:
                        errors += 1
                        logger.error(f"Failed to copy {video_file} to {local_file}: {e}")
                else:
                    files_skipped += 1
                    logger.debug(f"File already exists locally: {local_file}. Skipping.")
    
    # Log summary statistics
    logger.info(f"Cloud to local copy complete: {files_copied} files copied, {files_skipped} files skipped, {errors} errors")

def main():
    # Acquire lock
    acquire_lock(LOCKFILE_PATH)

    try:
        #we also need to check if every file in /web/uploads has thumbnail

        # process_unconverted_videos()
        copy_cloud_to_local_videos()
        # Process videos that need conversion

        # Process videos that need thumbnails
        process_thumbnail_null_videos()
        process_directory_for_thumbnails(UPLOADS_DIR)
        process_directory_for_thumbnails(OUTPUT_DIR_RAW)
        process_directory_for_thumbnails(TYD_DIR_POST)
        process_directory_for_thumbnails(OUTPUT_DIR_POST)


        # Update service records
        update_service_records(SERVICE_NAME, SERVICE_RECORDS_PATH)

        # Send success heartbeat
        monitor.send_heartbeat_with_stats(
            status="success",
            message="Video conversion and thumbnail generation completed",
            stats={
                "timestamp": datetime.now().isoformat()
            }
        )

    except Exception as e:
        # Send error heartbeat
        logger.error(f"Main process failed: {e}")
        monitor.send_heartbeat_with_stats(
            status="error",
            message=f"Video conversion failed: {str(e)}",
            stats={"error_type": type(e).__name__}
        )
        raise

    finally:
        pass


if __name__ == "__main__":
    main()
