"""Apple Vision OCR → stdout (для бенчмарку)."""
import sys, Quartz, Vision
from Foundation import NSURL
url = NSURL.fileURLWithPath_(sys.argv[1])
src = Quartz.CGImageSourceCreateWithURL(url, None)
img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
h = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
req = Vision.VNRecognizeTextRequest.alloc().init()
req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
req.setUsesLanguageCorrection_(True)
req.setRecognitionLanguages_(["en-US"])
h.performRequests_error_([req], None)
for o in req.results():
    c = o.topCandidates_(1)
    if c: print(c[0].string())
