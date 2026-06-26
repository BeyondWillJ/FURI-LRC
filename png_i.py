from PIL import Image, ImageChops

img = Image.open("icon.png").convert("RGBA")
r, g, b, a = img.split()
rgb = Image.merge("RGB", (r, g, b))
inverted = ImageChops.invert(rgb)
result = Image.merge("RGBA", (*inverted.split(), a))
result.save("icon-i.png")
