# 清洗./package*.bat文件
from pathlib import Path


def normalize_batch_file(path: Path) -> bool:
    data = path.read_bytes()
    # Work with bytes so the normalizer preserves a batch file's code page.
    # cmd.exe needs CRLF line endings, while its active code page can vary.
    normalized = data.removeprefix(b"\xef\xbb\xbf")
    normalized = normalized.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    output = normalized.replace(b"\n", b"\r\n")
    if output == data:
        return False
    path.write_bytes(output)
    return True


def main() -> None:
    files = sorted(Path(".").glob("package*.bat"))
    if not files:
        raise SystemExit("No package*.bat files found.")
    for path in files:
        status = "normalized" if normalize_batch_file(path) else "already clean"
        print(f"{path}: {status}")


if __name__ == "__main__":
    main()
