import json
import io
import asyncio
import tempfile
import os
import logging
import aiohttp
import aiofiles
import m3u8
import shutil # For moving the file and checking ffprobe
from urllib.parse import urljoin
from typing import Dict, Any, List, Tuple, Optional

# Assuming fetch_vk_video_info is correctly defined elsewhere
from services.content_downloaders.vk_services import fetch_vk_video_info

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Helper functions find_best_video_audio_formats, fetch_playlist, download_segment ---
# --- download_all_segments, create_ffmpeg_list_file, mux_streams remain the same ---
# (Copy them from the previous response if needed)
async def find_best_video_audio_formats(formats: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Selects the best HLS video-only and audio-only formats."""
    best_video = None
    best_audio = None
    max_video_res = 0
    max_audio_quality = -1

    for fmt in formats:
        if fmt.get('protocol') not in ('m3u8', 'm3u8_native'):
            continue

        is_video = fmt.get('vcodec') != 'none' and fmt.get('acodec') == 'none'
        is_audio = fmt.get('acodec') != 'none' and fmt.get('vcodec') == 'none'

        if is_video:
            height = fmt.get('height', 0)
            # Prefer formats with explicit resolution first
            if height > max_video_res:
                 max_video_res = height
                 best_video = fmt
            elif best_video is None: # Fallback if no video with height found yet
                 best_video = fmt
        elif is_audio:
            quality = fmt.get('quality', 0)
            if quality > max_audio_quality:
                 max_audio_quality = quality
                 best_audio = fmt
            elif best_audio is None: # Fallback
                 best_audio = fmt

    # Fallbacks if specific types weren't found but some HLS exists
    if not best_video and formats:
        logging.warning("No clear video-only HLS format found, trying best overall HLS video.")
        available_videos = [f for f in formats if f.get('vcodec') != 'none' and f.get('protocol') in ('m3u8', 'm3u8_native')]
        if available_videos:
            best_video = max(available_videos, key=lambda x: (x.get('height', 0), x.get('tbr', 0))) # Prioritize height, then bitrate

    if not best_audio and formats:
        logging.warning("No clear audio-only HLS format found, trying best overall HLS audio.")
        available_audios = [f for f in formats if f.get('acodec') != 'none' and f.get('protocol') in ('m3u8', 'm3u8_native')]
        if available_audios:
             audio_only_candidates = [f for f in available_audios if f.get('vcodec') == 'none']
             if audio_only_candidates:
                 best_audio = max(audio_only_candidates, key=lambda x: (x.get('quality', -1), x.get('abr', 0))) # Prioritize quality marker, then bitrate
             else:
                 # If no audio-only, pick best audio quality among combined streams
                 best_audio = max(available_audios, key=lambda x: (x.get('quality', -1), x.get('abr', 0)))


    return best_video, best_audio

async def fetch_playlist(session: aiohttp.ClientSession, url: str) -> Optional[m3u8.M3U8]:
    """Downloads and parses an M3U8 playlist."""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36'}
        async with session.get(url, headers=headers) as response:
            response.raise_for_status()
            playlist_content = await response.text()
            playlist = m3u8.loads(playlist_content, uri=url)

            if not playlist.segments:
                 logging.warning(f"Playlist {url} has no segments listed directly.")
                 if playlist.playlists:
                     logging.debug(f"Playlist {url} is a master playlist with variants.")
                     best_variant = max(playlist.playlists, key=lambda p: p.stream_info.bandwidth if p.stream_info else 0, default=None)
                     if best_variant and best_variant.uri:
                          variant_url = urljoin(url, best_variant.uri)
                          logging.debug(f"Fetching best variant playlist: {variant_url} (Bandwidth: {best_variant.stream_info.bandwidth if best_variant.stream_info else 'N/A'})")
                          return await fetch_playlist(session, variant_url)
                     else:
                         logging.error("Could not determine a suitable variant playlist URI from master playlist.")
                         return None
                 else:
                     logging.error("Playlist has no segments and is not a master playlist.")
                     return None
            return playlist
    except aiohttp.ClientError as e:
        logging.error(f"HTTP error fetching playlist {url}: {e}")
        return None
    except Exception as e:
        logging.error(f"Error parsing playlist {url}: {e}", exc_info=True)
        return None

async def download_segment(session: aiohttp.ClientSession, segment_uri: str, target_path: str) -> bool:
    """Downloads a single TS segment."""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36'}
        logging.debug(f"Downloading segment: {segment_uri}")
        async with session.get(segment_uri, headers=headers) as response:
            response.raise_for_status()
            async with aiofiles.open(target_path, 'wb') as f:
                while True:
                    chunk = await response.content.read(1024 * 1024)
                    if not chunk:
                        break
                    await f.write(chunk)
            logging.debug(f"Finished segment: {target_path}")
            return True
    except aiohttp.ClientError as e:
        status = getattr(e, 'status', 'N/A')
        logging.error(f"HTTP error {status} downloading segment {segment_uri}: {e}")
        return False
    except Exception as e:
        logging.error(f"Error writing segment {segment_uri} to {target_path}: {e}")
        return False

async def download_all_segments(session: aiohttp.ClientSession, playlist: m3u8.M3U8, target_dir: str) -> List[str]:
    """Downloads all segments from a playlist concurrently using absolute URIs."""
    tasks = []
    segment_paths = []
    if not playlist.segments:
        logging.error("Playlist object contains no segments to download.")
        return []
    for i, segment in enumerate(playlist.segments):
        segment_filename = f"segment_{i:05d}.ts"
        target_path = os.path.join(target_dir, segment_filename)
        absolute_segment_uri = segment.absolute_uri
        if not absolute_segment_uri:
             logging.warning(f"Segment {i} has no absolute URI. Skipping.")
             continue
        tasks.append(asyncio.create_task(download_segment(session, absolute_segment_uri, target_path)))
        segment_paths.append(target_path)
    results = await asyncio.gather(*tasks)
    successful_paths = [path for path, success in zip(segment_paths, results) if success]
    total_segments = len(playlist.segments)
    downloaded_count = len(successful_paths)
    if downloaded_count != total_segments:
        logging.warning(f"Downloaded {downloaded_count} out of {total_segments} segments.")
    else:
        logging.debug(f"Successfully downloaded all {total_segments} segments.")
    return successful_paths

async def create_ffmpeg_list_file(segment_paths: List[str], list_file_path: str):
    """Creates a file listing segments for FFmpeg concat demuxer."""
    async with aiofiles.open(list_file_path, 'w') as f:
        for path in segment_paths:
            abs_path_for_ffmpeg = os.path.abspath(path).replace('\\', '/')
            await f.write(f"file '{abs_path_for_ffmpeg}'\n")

async def mux_streams(video_list_path: str, audio_list_path: str, output_path: str) -> bool:
    """Uses FFmpeg to concatenate and mux video and audio streams."""
    command = [
        'ffmpeg', '-y',
        '-f', 'concat', '-safe', '0', '-i', video_list_path,
        '-f', 'concat', '-safe', '0', '-i', audio_list_path,
        '-c', 'copy',
        '-loglevel', 'warning', # Changed to warning for potentially more info on failure
        output_path
    ]
    logging.debug(f"Running FFmpeg command: {' '.join(command)}")
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        logging.error(f"FFmpeg failed with code {process.returncode}")
        logging.error(f"FFmpeg stderr:\n{stderr.decode(errors='ignore')}")
        return False
    else:
        logging.debug("FFmpeg muxing completed successfully.")
        return True

async def _validate_video_file(filepath: str) -> bool:
    """
    Uses ffprobe to perform a basic check if the video file is readable.
    Returns True if ffprobe succeeds, False otherwise.
    """
    if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
        logging.error(f"Validation failed: File '{filepath}' does not exist or is empty.")
        return False

    command = [
        'ffprobe',
        '-v', 'error', # Only show errors
        '-show_format', # Ask for format information
        '-show_streams', # Ask for stream information
        filepath
    ]
    logging.debug(f"Running validation command: {' '.join(command)}")
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE, # Capture stdout to check if it has content
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        logging.error(f"Validation failed: ffprobe exited with code {process.returncode}")
        logging.error(f"ffprobe stderr:\n{stderr.decode(errors='ignore')}")
        return False
    elif not stdout:
        logging.warning(f"Validation warning: ffprobe succeeded but produced no output for {filepath}. File might be valid but unusual.")
        # Consider returning True here if empty output is acceptable in some cases
        return True # Treat as success if ffprobe doesn't error, even w/o output
    else:
        logging.debug(f"Validation successful: ffprobe read metadata from '{filepath}'")
        # logging.debug(f"ffprobe output:\n{stdout.decode(errors='ignore')}") # Optional: log ffprobe output
        return True


async def _download_and_mux_hls_to_temp(json_data: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """
    Downloads and muxes HLS streams from json_data to a temporary file.

    Args:
        json_data: Dictionary containing video metadata and formats list.

    Returns:
        A tuple containing (path_to_muxed_temp_file, path_to_temp_dir) on success,
        or (None, None) on failure. The caller is responsible for cleaning up
        both the file and the directory.
    """
    video_format, audio_format = await find_best_video_audio_formats(json_data.get('formats', []))

    if not video_format or not video_format.get('url'):
        logging.error("Could not find a suitable HLS video format URL in JSON.")
        return None, None
    if not audio_format or not audio_format.get('url'):
        logging.error("Could not find a suitable HLS audio format URL in JSON.")
        return None, None

    video_m3u8_url = video_format['url']
    audio_m3u8_url = audio_format['url']
    logging.debug(f"Selected Video Stream: {video_format.get('format_id', 'N/A')} ({video_format.get('resolution', 'N/A')}) - Attempting fetch from {video_m3u8_url}")
    logging.debug(f"Selected Audio Stream: {audio_format.get('format_id', 'N/A')} - Attempting fetch from {audio_m3u8_url}")

    temp_dir = None
    muxed_temp_path = None
    video_list_file = None
    audio_list_file = None
    video_segment_dir = None
    audio_segment_dir = None

    try:
        temp_dir = tempfile.mkdtemp(prefix="hls_download_")
        logging.debug(f"Created temporary directory: {temp_dir}")
        video_segment_dir = os.path.join(temp_dir, "video_segments")
        audio_segment_dir = os.path.join(temp_dir, "audio_segments")
        os.makedirs(video_segment_dir, exist_ok=True)
        os.makedirs(audio_segment_dir, exist_ok=True)

        video_list_file = os.path.join(temp_dir, "videolist.txt")
        audio_list_file = os.path.join(temp_dir, "audiolist.txt")
        # Use a consistent temporary filename for the muxed output
        muxed_temp_path = os.path.join(temp_dir, "output_muxed.mp4")

        async with aiohttp.ClientSession() as session:
            # --- Download Video ---
            logging.debug("Fetching video playlist...")
            video_playlist = await fetch_playlist(session, video_m3u8_url)
            if not video_playlist: return None, temp_dir # Return temp_dir for cleanup

            logging.debug("Downloading video segments...")
            video_segment_paths = await download_all_segments(session, video_playlist, video_segment_dir)
            if not video_segment_paths:
                 logging.error("Failed to download required video segments.")
                 return None, temp_dir # Return temp_dir for cleanup
            await create_ffmpeg_list_file(video_segment_paths, video_list_file)
            logging.debug(f"Created video segment list: {video_list_file}")

            # --- Download Audio ---
            logging.debug("Fetching audio playlist...")
            audio_playlist = await fetch_playlist(session, audio_m3u8_url)
            if not audio_playlist: return None, temp_dir # Return temp_dir for cleanup

            logging.debug("Downloading audio segments...")
            audio_segment_paths = await download_all_segments(session, audio_playlist, audio_segment_dir)
            if not audio_segment_paths:
                logging.error("Failed to download required audio segments.")
                return None, temp_dir # Return temp_dir for cleanup
            await create_ffmpeg_list_file(audio_segment_paths, audio_list_file)
            logging.debug(f"Created audio segment list: {audio_list_file}")

        # --- Mux Streams ---
        logging.debug(f"Muxing video and audio streams using FFmpeg to {muxed_temp_path}...")
        mux_success = await mux_streams(video_list_file, audio_list_file, muxed_temp_path)
        if not mux_success:
            return None, temp_dir # Error logged in helper, return temp_dir for cleanup

        # Check if muxed file exists and is not empty before returning success
        if os.path.exists(muxed_temp_path) and os.path.getsize(muxed_temp_path) > 0:
            logging.debug(f"Successfully created temporary muxed file: {muxed_temp_path}")
            return muxed_temp_path, temp_dir # Return paths on success
        else:
            logging.error(f"Muxing appeared successful, but the output file {muxed_temp_path} is missing or empty.")
            return None, temp_dir # Return temp_dir for cleanup

    except Exception as e:
        logging.error(f"An error occurred during HLS download/muxing to temp: {e}", exc_info=True)
        # Even if an error occurred, return the temp_dir if it was created, so it can be cleaned up
        return None, temp_dir


async def download_manual_hls_to_file(json_data: Dict[str, Any], output_filepath: str) -> bool:
    """
    Downloads HLS streams described in JSON data, muxes them,
    and saves the result to output_filepath. Uses a temporary directory for intermediate files.

    Args:
        json_data: Dictionary containing video metadata and formats list.
        output_filepath: The full path where the final video file should be saved.

    Returns:
        True if download and muxing were successful and file saved, False otherwise.
    """
    muxed_temp_path = None
    temp_dir = None
    success = False

    try:
        # Ensure output directory exists
        output_dir = os.path.dirname(output_filepath)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        muxed_temp_path, temp_dir = await _download_and_mux_hls_to_temp(json_data)

        if muxed_temp_path and temp_dir:
            # --- Move to Final Location ---
            logging.debug(f"Moving temporary muxed file {muxed_temp_path} to final destination {output_filepath}")
            try:
                shutil.move(muxed_temp_path, output_filepath)
                logging.debug(f"Successfully moved video to {output_filepath}")
                success = True # Mark as successful *before* cleanup
                # muxed_temp_path should not exist anymore, so cleanup won't delete the final file
            except Exception as move_err:
                 logging.error(f"Failed to move {muxed_temp_path} to {output_filepath}: {move_err}")
                 success = False # Moving failed
        else:
            logging.error("Failed to create temporary muxed file.")
            success = False

        return success

    except Exception as e:
        logging.error(f"An error occurred during manual HLS download to file wrapper: {e}", exc_info=True)
        return False
    finally:
        # --- Cleanup ---
        # Clean up the temporary directory which contains segments, lists, etc.
        # The muxed file is either moved or should be cleaned up with the directory.
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
                logging.debug(f"Cleaned up temporary directory: {temp_dir}")
            except Exception as e:
                logging.error(f"Error cleaning up temp directory {temp_dir}: {e}")


async def download_video_as_bytes(video_info: dict) -> Optional[bytes]:
    """
    Downloads a video from the given URL using the HLS method,
    muxes it, reads the content into bytes, and returns it.

    Args:
        video_info: json data containing video metadata

    Returns:
        The video content as bytes if successful, None otherwise.
    """
    muxed_temp_path = None
    temp_dir = None
    video_bytes = None

    try:
        muxed_temp_path, temp_dir = await _download_and_mux_hls_to_temp(video_info)

        if muxed_temp_path and temp_dir:
            # 3. Read bytes from temporary file
            logging.debug(f"Reading bytes from temporary file: {muxed_temp_path}")
            try:
                async with aiofiles.open(muxed_temp_path, 'rb') as f:
                    video_bytes = await f.read()
                logging.debug(f"Successfully read {len(video_bytes)} bytes from {muxed_temp_path}")
                # Optional: Validate bytes (e.g., check if non-empty)
                if not video_bytes:
                     logging.warning(f"Read 0 bytes from {muxed_temp_path}, returning None.")
                     return None # Or handle as appropriate

            except Exception as read_err:
                logging.error(f"Failed to read bytes from {muxed_temp_path}: {read_err}")
                return None # Failed to read bytes
        else:
            logging.error("Failed to create temporary muxed file for byte reading.")
            return None # Muxing/download failed
        return video_bytes # Return the read bytes

    except Exception as e:
        logging.error(f"An error occurred during video download to bytes for {url}: {e}", exc_info=True)
        return None # General failure
    finally:
        # --- Cleanup ---
        # Clean up the entire temporary directory, including the muxed file
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
                logging.debug(f"Cleaned up temporary directory: {temp_dir}")
            except Exception as e:
                logging.error(f"Error cleaning up temp directory {temp_dir}: {e}")


# --- Example Usage ---
async def main():
    target_url = 'https://vimeo.com/1038351309'
    # Define where you want to save the final video if using file download
    final_video_path = "./downloaded_video.mp4" # Save in current directory

    # --- Option 1: Download to File ---
    # try:
    #     video_info: dict = await fetch_vk_video_info(target_url)
    #     if not video_info or 'formats' not in video_info:
    #          logging.error("Failed to get valid video info from API.")
    #          return
    #     logging.debug()(f"Attempting manual HLS download to file: {final_video_path}")
    #     download_successful = await download_manual_hls_to_file(video_info, final_video_path)
    #     if download_successful:
    #          logging.debug()(f"Manual download process completed. Verifying file: {final_video_path}")
    #          is_valid = await _validate_video_file(final_video_path)
    #          if is_valid:
    #              logging.debug()(f"File '{final_video_path}' appears valid.")
    #          else:
    #              logging.error(f"File '{final_video_path}' failed validation (check logs).")
    #     else:
    #          logging.error("Manual HLS download failed.")
    # except Exception as e:
    #     logging.error(f"An error occurred in the main workflow (file download): {e}", exc_info=True)

    # --- Option 2: Download to Bytes ---
    try:
        vidoe_info = await fetch_vk_video_info(target_url)
        logging.debug(f"Attempting to download video as bytes from: {target_url}")
        video_data = await download_video_as_bytes(vidoe_info)

        if video_data:
            logging.debug(f"Successfully downloaded video data: {len(video_data)} bytes.")
            # You can now process video_data, e.g., save it, stream it, etc.
            # Example: Save the bytes to a file
            bytes_output_path = "./downloaded_video_from_bytes.mp4"
            try:
                with open(bytes_output_path, 'wb') as f:
                    f.write(video_data)
                logging.debug(f"Saved video bytes to {bytes_output_path}")
                # Optionally validate the saved file
                is_valid = await _validate_video_file(bytes_output_path)
                if is_valid:
                     logging.debug(f"File '{bytes_output_path}' saved from bytes appears valid.")
                else:
                     logging.error(f"File '{bytes_output_path}' saved from bytes failed validation.")

            except Exception as write_err:
                logging.error(f"Failed to write video bytes to file: {write_err}")

        else:
            logging.error("Failed to download video as bytes.")

    except Exception as e:
        logging.error(f"An error occurred in the main workflow (bytes download): {e}", exc_info=True)


if __name__ == "__main__":
    # Ensure FFmpeg and ffprobe are installed and in PATH
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
         logging.error("FATAL: FFmpeg or ffprobe command not found in PATH. Manual download/validation cannot proceed.")
    else:
        logging.debug(f"FFmpeg found at: {shutil.which('ffmpeg')}")
        logging.debug(f"ffprobe found at: {shutil.which('ffprobe')}")
        asyncio.run(main())