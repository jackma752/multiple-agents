"""Deterministic format/size conversion of the supplied project artwork.

No crop, retouch, generation or text removal. Requires Pillow for asset builds,
not for running or packaging the application.
"""
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]


def main():
    source = ROOT / 'assets/branding/source.png'
    destination = ROOT / 'apps/api/agenthub_rt/static/brand'
    destination.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image = image.convert('RGBA')
        if image.width != image.height:
            raise ValueError('Use a square source; never crop the user artwork implicitly')
        for size in (64, 256, 512):
            image.resize((size,size),Image.Resampling.LANCZOS).save(
                destination / f'app-icon-{size}.png', optimize=True)
        image.resize((256,256),Image.Resampling.LANCZOS).save(destination/'app.ico',
            format='ICO',sizes=[(s,s) for s in (16,24,32,48,64,128,256)])


if __name__ == '__main__':
    main()
