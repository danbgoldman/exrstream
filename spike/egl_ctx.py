"""Headless OpenGL on the NVIDIA device via EGL, with no X and no GL headers.

PyOpenGL is ctypes over the runtime libEGL/libGL, so this needs no -dev packages
(none are installed and sudo wants a password). Uses the EGL device platform
extension to bind the GPU directly, surfaceless.
"""
import ctypes, os
os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
from OpenGL import EGL
from OpenGL.EGL import *

def make_context(gl_major=4, gl_minor=3):
    getdevs = ctypes.cast(eglGetProcAddress(b"eglQueryDevicesEXT"),
                          ctypes.CFUNCTYPE(EGLBoolean, ctypes.c_int,
                                           ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)))
    getdisp = ctypes.cast(eglGetProcAddress(b"eglGetPlatformDisplayEXT"),
                          ctypes.CFUNCTYPE(EGLDisplay, ctypes.c_uint,
                                           ctypes.c_void_p, ctypes.c_void_p))
    n = ctypes.c_int()
    getdevs(0, None, ctypes.byref(n))
    devs = (ctypes.c_void_p * n.value)()
    getdevs(n.value, devs, ctypes.byref(n))

    EGL_PLATFORM_DEVICE_EXT = 0x313F
    for i in range(n.value):
        dpy = getdisp(EGL_PLATFORM_DEVICE_EXT, devs[i], None)
        if not dpy:
            continue
        maj, mnr = EGLint(), EGLint()
        if not eglInitialize(dpy, maj, mnr):
            continue
        eglBindAPI(EGL_OPENGL_API)
        cfgs, ncfg = (EGLConfig * 1)(), EGLint()
        attrs = [EGL_SURFACE_TYPE, EGL_PBUFFER_BIT, EGL_RENDERABLE_TYPE,
                 EGL_OPENGL_BIT, EGL_NONE]
        eglChooseConfig(dpy, (EGLint * len(attrs))(*attrs), cfgs, 1, ncfg)
        ctx_attrs = [0x3098, gl_major, 0x30FB, gl_minor,
                     0x30FD, 0x00000001, EGL_NONE]   # MAJOR, MINOR, EGL_CONTEXT_OPENGL_PROFILE_MASK=CORE
        ctx = eglCreateContext(dpy, cfgs[0] if ncfg.value else None,
                               EGL_NO_CONTEXT, (EGLint * len(ctx_attrs))(*ctx_attrs))
        if ctx == EGL_NO_CONTEXT:
            continue
        if eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, ctx):
            return dpy, ctx, i
    raise RuntimeError(f"no EGL device gave a context ({n.value} devices tried)")

if __name__ == "__main__":
    from OpenGL.GL import glGetString, GL_RENDERER, GL_VERSION, GL_SHADING_LANGUAGE_VERSION
    dpy, ctx, i = make_context()
    print("device", i)
    for e in (GL_RENDERER, GL_VERSION, GL_SHADING_LANGUAGE_VERSION):
        print(" ", glGetString(e).decode())
