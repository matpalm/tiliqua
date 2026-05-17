from picamera2 import Picamera2
import time
from PIL import Image

picam = Picamera2()

# config = picam.create_still_configuration(main={"size": (128, 75)})
# picam.configure(config)

picam.start()
time.sleep(2)  # warmup

# full res
output_fname = "image.full.png"
picam.capture_file(output_fname)

# explicit resize to (128, 75) with PIL
Image.open(output_fname).resize((128, 75)).save("image.small.png")

picam.stop()
