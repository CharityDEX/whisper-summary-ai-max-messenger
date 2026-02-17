from services.content_downloaders.vk_services import fetch_vk_video_info
from services.content_downloaders.yt_dlp_downloader import download_video_as_bytes

async def download_vimeo_video(url: str) -> bytes:
    video_info: dict = await fetch_vk_video_info(url=url)
    video_bytes: bytes = await download_video_as_bytes(video_info)
    return video_bytes