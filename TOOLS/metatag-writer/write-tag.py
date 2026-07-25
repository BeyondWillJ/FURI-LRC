"""
将元信息（含封面）写入 MP3 文件（ID3 v2.3/v2.4）
依赖: pip install mutagen --break-system-packages
"""

from mutagen.id3 import (
    ID3, ID3NoHeaderError,
    TIT2, TPE1, TALB, TPE2, TCON, TDRC, TRCK, TPOS,
    TSRC, TXXX, USLT, APIC,
)
from mutagen.mp3 import MP3


def write_mp3_metadata(
    mp3_path: str,
    title: str = None,
    artist: str = None,
    album: str = None,
    album_artist: str = None,
    genre: str = None,
    year: str = None,          # 如 "2024" 或 "2024-05-01"
    track_number: str = None,  # 如 "3" 或 "3/12"
    disc_number: str = None,   # 如 "1/1"
    isrc: str = None,
    lyrics: str = None,        # 未同步歌词，写入 USLT
    cover_path: str = None,    # 封面图片路径 (jpg/png)
    cover_mime: str = "image/jpeg",  # png 用 "image/png"
):
    # 加载或新建 ID3 容器
    try:
        tags = ID3(mp3_path)
    except ID3NoHeaderError:
        tags = ID3()

    if title:
        tags.setall("TIT2", [TIT2(encoding=3, text=title)])
    if artist:
        tags.setall("TPE1", [TPE1(encoding=3, text=artist)])
    if album:
        tags.setall("TALB", [TALB(encoding=3, text=album)])
    if album_artist:
        tags.setall("TPE2", [TPE2(encoding=3, text=album_artist)])
    if genre:
        tags.setall("TCON", [TCON(encoding=3, text=genre)])
    if year:
        tags.setall("TDRC", [TDRC(encoding=3, text=year)])
    if track_number:
        tags.setall("TRCK", [TRCK(encoding=3, text=track_number)])
    if disc_number:
        tags.setall("TPOS", [TPOS(encoding=3, text=disc_number)])
    if isrc:
        tags.setall("TSRC", [TSRC(encoding=3, text=isrc)])
    if lyrics:
        # lang 用 ISO 639-2 三位码，中文 "chi"，日文 "jpn"
        tags.setall("USLT", [USLT(encoding=3, lang="jpn", desc="", text=lyrics)])

    if cover_path:
        with open(cover_path, "rb") as f:
            cover_data = f.read()
        # 先清掉旧封面，避免重复
        tags.delall("APIC")
        tags.add(
            APIC(
                encoding=3,       # UTF-8
                mime=cover_mime,  # image/jpeg 或 image/png
                type=3,           # 3 = 封面 (front cover)
                desc="Cover",
                data=cover_data,
            )
        )

    # v2.3 兼容性最好（Windows 资源管理器/旧播放器都能读）
    tags.save(mp3_path, v2_version=3)
    print(f"已写入: {mp3_path}")


if __name__ == "__main__":
    write_mp3_metadata(
        mp3_path=r"F:\FURI-LRC-APP\furi-lrc-player\songs\sisters noise.mp3",
        # title="曲名",
        # artist="艺术家",
        # album="专辑名",
        # album_artist="专辑艺术家",
        # genre="J-Pop",
        # year="2024",
        # track_number="3/12",
        cover_path="sisters-noise.jpg",
        cover_mime="image/jpeg",
    )