"""Headless GPU grade: OCIO's own generated ACES shader on an EGL context.

Promoted from spike/07_ocio_gpu.py. The two decisions that matter, both measured
in Phase 0 (see spike/RESULTS.md):

  * Run OCIO's shader, not a baked 3D LUT. A 3D LUT cannot represent the ACES
    2.0 output transform -- its gamut compressor clips channels to exactly 0
    along a surface in the colour cube, and trilinear interpolation smears that
    discontinuity with only O(1/n) convergence (129^3 still misses by 33/255).
  * Exposure is an OCIO *dynamic property*, i.e. a plain uniform. A slider move
    sets one float. Later pipeline stages are extra transforms in the group and
    OCIO regenerates the shader, so they stay correct by construction.
"""
import ctypes, os
import numpy as np
import PyOpenColorIO as ocio

os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
from OpenGL.GL import *          # noqa: E402
from OpenGL.EGL import *         # noqa: E402

CONFIG = "ocio://cg-config-v4.0.0_aces-v2.0_ocio-v2.5"
DISPLAY = "sRGB - Display"
VIEW = "ACES 2.0 - SDR 100 nits (Rec.709)"
DEFAULT_SRC = "ACEScg"

_cfg = None
_ctx = None


def config():
    global _cfg
    if _cfg is None:
        _cfg = ocio.Config.CreateFromFile(CONFIG)
    return _cfg


def colorspaces():
    return [cs.getName() for cs in config().getColorSpaces()]


def views(display=DISPLAY):
    return list(config().getViews(display))


def displays():
    return list(config().getDisplays())


def make_context(gl_major=4, gl_minor=3):
    """Bind the GPU directly via the EGL device platform: no X, no dev headers.

    PyOpenGL is ctypes over the runtime libEGL, so this needs no -dev packages.
    """
    global _ctx
    if _ctx is not None:
        return _ctx
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
        attrs = [EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
                 EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT, EGL_NONE]
        eglChooseConfig(dpy, (EGLint * len(attrs))(*attrs), cfgs, 1, ncfg)
        ctx_attrs = [0x3098, gl_major, 0x30FB, gl_minor,
                     0x30FD, 0x00000001, EGL_NONE]   # MAJOR, MINOR, PROFILE=CORE
        ctx = eglCreateContext(dpy, cfgs[0] if ncfg.value else None,
                               EGL_NO_CONTEXT, (EGLint * len(ctx_attrs))(*ctx_attrs))
        if ctx == EGL_NO_CONTEXT:
            continue
        if eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, ctx):
            _ctx = (dpy, ctx)
            return _ctx
    raise RuntimeError(f"no EGL device gave a GL context ({n.value} devices tried)")


_VERT = """#version 430 core
void main(){ vec2 p = vec2((gl_VertexID<<1)&2, gl_VertexID&2); gl_Position = vec4(p*2.0-1.0,0,1); }
"""


def _compile(src, kind):
    s = glCreateShader(kind)
    glShaderSource(s, src)
    glCompileShader(s)
    if not glGetShaderiv(s, GL_COMPILE_STATUS):
        raise RuntimeError(glGetShaderInfoLog(s).decode()[:2000])
    return s


class Grade:
    """Linear scene-referred RGBA half in, display-referred BGRA uint8 out.

    BGRA because that is byte-for-byte NVENC's ARGB. Do not also swizzle in the
    shader; `glReadPixels(GL_BGRA)` on an RGBA framebuffer already lands as
    B,G,R,A and the two would cancel.
    """

    def __init__(self, w, h, src=DEFAULT_SRC, display=DISPLAY, view=VIEW):
        make_context()
        self.w, self.h, self.src = w, h, src
        self.display, self.view = display, view

        grp = ocio.GroupTransform()
        grp.appendTransform(ocio.ExposureContrastTransform(
            style=ocio.EXPOSURE_CONTRAST_LINEAR, exposure=0.0, dynamicExposure=True))
        # NOT getProcessor(src, "sRGB - Display"): that is a colourspace
        # conversion -- primaries matrix plus transfer function, no tone mapping
        # and no gamut compression, so 0.18 grey lands at 0.46 instead of 0.35.
        # The ACES output transform lives on the *view*.
        grp.appendTransform(ocio.DisplayViewTransform(
            src=src, display=display, view=view))
        self.proc = config().getProcessor(grp)

        self.desc = ocio.GpuShaderDesc.CreateShaderDesc(
            language=ocio.GPU_LANGUAGE_GLSL_4_0)
        self.proc.getDefaultGPUProcessor().extractGpuShaderInfo(self.desc)

        frag = f"""#version 430 core
uniform sampler2D img;
uniform vec2 res;
out vec4 fragColor;
{self.desc.getShaderText()}
void main(){{
    vec4 c = texture(img, gl_FragCoord.xy/res);
    fragColor = {self.desc.getFunctionName()}(c);
}}"""
        p = glCreateProgram()
        for s in (_compile(_VERT, GL_VERTEX_SHADER), _compile(frag, GL_FRAGMENT_SHADER)):
            glAttachShader(p, s)
        glLinkProgram(p)
        if not glGetProgramiv(p, GL_LINK_STATUS):
            raise RuntimeError(glGetProgramInfoLog(p).decode()[:2000])
        self.prog = p
        glUseProgram(p)

        self.unit, self._luts = 1, []
        for t in self.desc.getTextures():
            self._lut(t, 2 if t.height > 1 else 1)
        for t in self.desc.get3DTextures():
            self._lut3d(t)

        self.vao = glGenVertexArrays(1)
        self.tex = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, self.tex)
        for k in (GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER):
            glTexParameteri(GL_TEXTURE_2D, k, GL_NEAREST)
        # RGBA16F, not RGB16F: a 3-component upload makes the driver repack
        # every row -- 1.06 GB/s vs 21 GB/s, i.e. 44ms vs 2.9ms for a 4K frame.
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA16F, w, h, 0, GL_RGBA, GL_HALF_FLOAT, None)

        self.fbo = glGenFramebuffers(1)
        self.out = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, self.out)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, None)
        for k in (GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER):
            glTexParameteri(GL_TEXTURE_2D, k, GL_NEAREST)
        glBindFramebuffer(GL_FRAMEBUFFER, self.fbo)
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, self.out, 0)
        assert glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE

        glUniform1i(glGetUniformLocation(self.prog, "img"), 0)
        glUniform2f(glGetUniformLocation(self.prog, "res"), float(w), float(h))
        self._ev_prop = self.desc.getDynamicProperty(ocio.DYNAMIC_PROPERTY_EXPOSURE)
        self.cpu = self.proc.getDefaultCPUProcessor()
        self._cpu_prop = self.cpu.getDynamicProperty(ocio.DYNAMIC_PROPERTY_EXPOSURE)
        self.ev = None
        self.set_exposure(0.0)

    def _lut(self, t, dim):
        vals = np.asarray(t.getValues(), np.float32)
        nchan = 3 if t.channel == ocio.GpuShaderDesc.TEXTURE_RGB_CHANNEL else 1
        fmt, ifmt = (GL_RGB, GL_RGB32F) if nchan == 3 else (GL_RED, GL_R32F)
        tid = glGenTextures(1)
        glActiveTexture(GL_TEXTURE0 + self.unit)
        tgt = GL_TEXTURE_1D if dim == 1 else GL_TEXTURE_2D
        glBindTexture(tgt, tid)
        if dim == 1:
            glTexImage1D(tgt, 0, ifmt, t.width, 0, fmt, GL_FLOAT, vals)
        else:
            glTexImage2D(tgt, 0, ifmt, t.width, t.height, 0, fmt, GL_FLOAT, vals)
        interp = GL_NEAREST if t.interpolation == ocio.INTERP_NEAREST else GL_LINEAR
        glTexParameteri(tgt, GL_TEXTURE_MIN_FILTER, interp)
        glTexParameteri(tgt, GL_TEXTURE_MAG_FILTER, interp)
        glTexParameteri(tgt, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        glUniform1i(glGetUniformLocation(self.prog, t.samplerName), self.unit)
        self._luts.append(tid); self.unit += 1

    def _lut3d(self, t):
        vals = np.asarray(t.getValues(), np.float32)
        tid = glGenTextures(1)
        glActiveTexture(GL_TEXTURE0 + self.unit)
        glBindTexture(GL_TEXTURE_3D, tid)
        n = t.edgeLen
        glTexImage3D(GL_TEXTURE_3D, 0, GL_RGB32F, n, n, n, 0, GL_RGB, GL_FLOAT, vals)
        for k in (GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER):
            glTexParameteri(GL_TEXTURE_3D, k, GL_LINEAR)
        for k in (GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T, GL_TEXTURE_WRAP_R):
            glTexParameteri(GL_TEXTURE_3D, k, GL_CLAMP_TO_EDGE)
        glUniform1i(glGetUniformLocation(self.prog, t.samplerName), self.unit)
        self._luts.append(tid); self.unit += 1

    def set_exposure(self, ev):
        if ev == self.ev:
            return
        self.ev = ev
        self._ev_prop.setDouble(float(ev))
        self._cpu_prop.setDouble(float(ev))
        glUseProgram(self.prog)
        for name, u in self.desc.getUniforms():
            if u.type == ocio.UNIFORM_DOUBLE:
                glUniform1f(glGetUniformLocation(self.prog, name), float(u.getDouble()))

    def __call__(self, rgba):
        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D, self.tex)
        glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, self.w, self.h,
                        GL_RGBA, GL_HALF_FLOAT, rgba)
        glBindFramebuffer(GL_FRAMEBUFFER, self.fbo)
        glViewport(0, 0, self.w, self.h)
        glUseProgram(self.prog)
        glBindVertexArray(self.vao)
        glDrawArrays(GL_TRIANGLES, 0, 3)
        buf = glReadPixels(0, 0, self.w, self.h, GL_BGRA, GL_UNSIGNED_BYTE)
        return np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 4)


_pool = {}


def grade_for(w, h, src=DEFAULT_SRC, display=DISPLAY, view=VIEW):
    """Shared per (size, colourspace, view). The grade carries no per-viewer
    state except the exposure uniform, which is set immediately before each
    render, so sessions can share one and skip duplicating shaders and FBOs."""
    key = (w, h, src, display, view)
    if key not in _pool:
        _pool[key] = Grade(w, h, src, display, view)
    return _pool[key]
