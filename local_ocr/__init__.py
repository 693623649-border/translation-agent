"""Local OCR service implementation.

PaddlePaddle is imported only inside GPU worker processes so importing the
main translation agent never requires the optional GPU environment.
"""
