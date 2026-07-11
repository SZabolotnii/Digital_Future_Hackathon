import sys, Quartz, Vision, statistics
from Foundation import NSURL
for p in sys.argv[1:]:
    url=NSURL.fileURLWithPath_(p); src=Quartz.CGImageSourceCreateWithURL(url,None)
    img=Quartz.CGImageSourceCreateImageAtIndex(src,0,None)
    h=Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img,None)
    r=Vision.VNRecognizeTextRequest.alloc().init()
    r.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    r.setUsesLanguageCorrection_(True); r.setRecognitionLanguages_(["en-US"])
    h.performRequests_error_([r],None)
    cs=[o.topCandidates_(1)[0].confidence() for o in r.results() if o.topCandidates_(1)]
    low=sum(1 for c in cs if c<0.5)
    print(f"{p:20s} блоків={len(cs):4d}  сер.conf={statistics.mean(cs):.3f}  "
          f"мін={min(cs):.2f}  частка conf<0.5: {low/len(cs):.0%}")
