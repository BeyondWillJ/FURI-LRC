from PIL import Image
import sys
import os

def png2ico(input_path="icon.png", output_path=None):
    if output_path is None:
        output_path = os.path.splitext(input_path)[0] + ".ico"

    img = Image.open(input_path).convert("RGBA")
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(output_path, format="ICO", sizes=sizes)
    print(f"Saved: {output_path}")

if __name__ == "__main__":
    png2ico("icon-i.png", "icon-i.ico")
