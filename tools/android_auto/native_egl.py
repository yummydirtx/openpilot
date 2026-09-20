"""EGL pbuffer for the actual 3X widgets, without a window or DRM ownership.

Only used in the isolated projection worker. The native four process retains
display power, touch input and its normal fail-open display watchdog.
"""

import ctypes as C
import os
import time


class HeadlessContext:
  def __init__(self, width, height):
    self.egl = C.CDLL("libEGL.so")
    signatures = {
      "eglGetDisplay": (C.c_void_p, [C.c_void_p]),
      "eglInitialize": (C.c_uint, [C.c_void_p, C.POINTER(C.c_int), C.POINTER(C.c_int)]),
      "eglBindAPI": (C.c_uint, [C.c_uint]),
      "eglChooseConfig": (C.c_uint, [C.c_void_p, C.POINTER(C.c_int), C.POINTER(C.c_void_p), C.c_int, C.POINTER(C.c_int)]),
      "eglCreatePbufferSurface": (C.c_void_p, [C.c_void_p, C.c_void_p, C.POINTER(C.c_int)]),
      "eglCreateContext": (C.c_void_p, [C.c_void_p, C.c_void_p, C.c_void_p, C.POINTER(C.c_int)]),
      "eglMakeCurrent": (C.c_uint, [C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p]),
      "eglDestroySurface": (C.c_uint, [C.c_void_p, C.c_void_p]),
      "eglDestroyContext": (C.c_uint, [C.c_void_p, C.c_void_p]),
      "eglTerminate": (C.c_uint, [C.c_void_p]),
    }
    for name, (result, args) in signatures.items():
      f = getattr(self.egl, name)
      f.restype, f.argtypes = result, args
    # Qualcomm's EGL expects a GBM native display (EGL_DEFAULT_DISPLAY is not
    # supported). Opening the render node does not acquire DRM master or touch
    # the four's scanout/connector.
    self.gbm = C.CDLL("libgbm.so")
    self.gbm.gbm_create_device.argtypes = [C.c_int]
    self.gbm.gbm_create_device.restype = C.c_void_p
    self.gbm.gbm_device_destroy.argtypes = [C.c_void_p]
    self.drm_fd = os.open("/dev/dri/renderD128", os.O_RDWR | os.O_CLOEXEC)
    self.gbm_device = self.gbm.gbm_create_device(self.drm_fd)
    self.check(self.gbm_device, "GBM device")
    self.display = self.egl.eglGetDisplay(self.gbm_device)
    self.context = self.surface = None
    major, minor, count, config = C.c_int(), C.c_int(), C.c_int(), C.c_void_p()
    self.check(self.egl.eglInitialize(self.display, C.byref(major), C.byref(minor)), "initialize")
    self.check(self.egl.eglBindAPI(0x30A0), "bind GLES")
    attrs = (C.c_int * 13)(0x3033, 1, 0x3040, 0x0040, 0x3024, 8, 0x3023, 8, 0x3022, 8, 0x3021, 8, 0x3038)
    self.check(self.egl.eglChooseConfig(self.display, attrs, C.byref(config), 1, C.byref(count)) and count.value, "config")
    self.surface = self.egl.eglCreatePbufferSurface(self.display, config, (C.c_int * 5)(0x3057, width, 0x3056, height, 0x3038))
    self.check(self.surface, "pbuffer")
    self.context = self.egl.eglCreateContext(self.display, config, None, (C.c_int * 3)(0x3098, 3, 0x3038))
    self.check(self.context, "context")
    self.check(self.egl.eglMakeCurrent(self.display, self.surface, self.surface, self.context), "make current")
    import pyray as rl
    self.rl = rl
    rl.rl_load_extensions(rl.ffi.cast("void *", C.cast(self.egl.eglGetProcAddress, C.c_void_p).value))
    rl.rlgl_init(width, height)
    rl.rl_set_framebuffer_width(width)
    rl.rl_set_framebuffer_height(height)
    texture = rl.Texture(rl.rl_get_texture_id_default(), 1, 1, 1, 7)
    rl.set_shapes_texture(texture, rl.Rectangle(0, 0, 1, 1))
    # No native raylib event/timer loop is started for a pbuffer.
    rl.get_time = time.monotonic
    rl.get_frame_time = lambda: 1 / 30

  def check(self, value, operation):
    if not value:
      raise RuntimeError(f"Headless EGL {operation} failed: {self.egl.eglGetError():#x}")

  def close(self):
    self.rl.rlgl_close()
    self.egl.eglMakeCurrent(self.display, None, None, None)
    self.egl.eglDestroyContext(self.display, self.context)
    self.egl.eglDestroySurface(self.display, self.surface)
    self.egl.eglTerminate(self.display)
    self.gbm.gbm_device_destroy(self.gbm_device)
    os.close(self.drm_fd)
