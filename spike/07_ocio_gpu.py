"""Phase 0 #5 (correct): run OCIO's OWN generated shader on the GPU.

A 3D LUT cannot represent the ACES 2.0 output transform: its gamut compressor
clips channels to exactly 0 along a surface in the colour cube, and trilinear
interpolation smears that discontinuity. Error falls only as O(1/n), so 129^3
still misses by 32/255 and reaching 2/255 would need n~2000.

OCIO emits the real ACES 2.0 math instead (~300 lines GLSL + two small 1D
tables, no 3D LUT), so running its shader is both exact and free of the whole
problem. Exposure is an OCIO *dynamic property*, i.e. a plain uniform -- a
slider move sets a float, with no shader rebuild and no LUT re-bake. Extra
pipeline stages later are extra transforms in the group; OCIO regenerates the
shader and it stays correct by construction.
"""
import ctypes, time
import numpy as np
import PyOpenColorIO as ocio
from OpenGL.GL import *
from egl_ctx import make_context

CONFIG = "ocio://cg-config-v4.0.0_aces-v2.0_ocio-v2.5"
SRC = "ACEScg"
DISPLAY, VIEW = "sRGB - Display", "ACES 2.0 - SDR 100 nits (Rec.709)"

VERT = """#version 430 core
void main(){ vec2 p = vec2((gl_VertexID<<1)&2, gl_VertexID&2); gl_Position = vec4(p*2.0-1.0,0,1); }
"""

def build_processor():
    cfg = ocio.Config.CreateFromFile(CONFIG)
    grp = ocio.GroupTransform()
    ec = ocio.ExposureContrastTransform(style=ocio.EXPOSURE_CONTRAST_LINEAR,
                                        exposure=0.0, dynamicExposure=True)
    grp.appendTransform(ec)
    grp.appendTransform(ocio.DisplayViewTransform(src=SRC, display=DISPLAY, view=VIEW))
    return cfg.getProcessor(grp)

def compile_shader(src, kind):
    s = glCreateShader(kind); glShaderSource(s, src); glCompileShader(s)
    if not glGetShaderiv(s, GL_COMPILE_STATUS):
        raise RuntimeError(glGetShaderInfoLog(s).decode()[:2000])
    return s

class OcioGL:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.proc = build_processor()
        gpu = self.proc.getDefaultGPUProcessor()
        self.desc = ocio.GpuShaderDesc.CreateShaderDesc(language=ocio.GPU_LANGUAGE_GLSL_4_0)
        gpu.extractGpuShaderInfo(self.desc)

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
        for s in (compile_shader(VERT, GL_VERTEX_SHADER), compile_shader(frag, GL_FRAGMENT_SHADER)):
            glAttachShader(p, s)
        glLinkProgram(p)
        if not glGetProgramiv(p, GL_LINK_STATUS):
            raise RuntimeError(glGetProgramInfoLog(p).decode()[:2000])
        self.prog = p
        glUseProgram(p)

        # OCIO's lookup tables -> texture units 1..n (unit 0 is the image)
        self.unit = 1
        self._luts = []
        for t in self.desc.getTextures():
            self._upload_lut(t, dim=2 if t.height > 1 else 1)
        for t in self.desc.get3DTextures():
            self._upload_lut3d(t)

        self.vao = glGenVertexArrays(1)
        self.tex = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, self.tex)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA16F, w, h, 0, GL_RGBA, GL_HALF_FLOAT, None)

        self.fbo = glGenFramebuffers(1)
        self.out = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, self.out)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, None)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
        glBindFramebuffer(GL_FRAMEBUFFER, self.fbo)
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, self.out, 0)
        assert glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE

        glUniform1i(glGetUniformLocation(self.prog, "img"), 0)
        glUniform2f(glGetUniformLocation(self.prog, "res"), float(w), float(h))
        self.exposure_prop = self.desc.getDynamicProperty(ocio.DYNAMIC_PROPERTY_EXPOSURE)
        self.cpu = self.proc.getDefaultCPUProcessor()
        self.cpu_prop = self.cpu.getDynamicProperty(ocio.DYNAMIC_PROPERTY_EXPOSURE)

    def _tex_target(self, dim):
        return GL_TEXTURE_1D if dim == 1 else GL_TEXTURE_2D

    def _upload_lut(self, t, dim):
        vals = np.asarray(t.getValues(), np.float32)
        nchan = 3 if t.channel == ocio.GpuShaderDesc.TEXTURE_RGB_CHANNEL else 1
        fmt, ifmt = (GL_RGB, GL_RGB32F) if nchan == 3 else (GL_RED, GL_R32F)
        tid = glGenTextures(1)
        glActiveTexture(GL_TEXTURE0 + self.unit)
        tgt = self._tex_target(dim)
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

    def _upload_lut3d(self, t):
        vals = np.asarray(t.getValues(), np.float32)
        tid = glGenTextures(1)
        glActiveTexture(GL_TEXTURE0 + self.unit)
        glBindTexture(GL_TEXTURE_3D, tid)
        n = t.edgeLen
        glTexImage3D(GL_TEXTURE_3D, 0, GL_RGB32F, n, n, n, 0, GL_RGB, GL_FLOAT, vals)
        for p in (GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER):
            glTexParameteri(GL_TEXTURE_3D, p, GL_LINEAR)
        for p in (GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T, GL_TEXTURE_WRAP_R):
            glTexParameteri(GL_TEXTURE_3D, p, GL_CLAMP_TO_EDGE)
        glUniform1i(glGetUniformLocation(self.prog, t.samplerName), self.unit)
        self._luts.append(tid); self.unit += 1

    def set_exposure(self, ev):
        self.exposure_prop.setDouble(float(ev))
        self.cpu_prop.setDouble(float(ev))
        glUseProgram(self.prog)
        for name, u in self.desc.getUniforms():
            if u.type == ocio.UNIFORM_DOUBLE:
                glUniform1f(glGetUniformLocation(self.prog, name), float(u.getDouble()))

    def __call__(self, rgba):
        """rgba: (h,w,4) float16 linear ACEScg -> (h,w,3) uint8 display.

        RGBA half, not RGB half. The 4th channel is pure padding and costs 33%
        more memory, but a 3-component upload makes the driver repack every row:
        1.06 GB/s vs 21 GB/s, i.e. 44ms vs 2.9ms for a 4K frame. So the frame
        cache stores RGBA and the padding is paid once at decode, not per frame.
        A PBO is slower still here (9 GB/s), so there is no async path to add.
        """
        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D, self.tex)
        glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, self.w, self.h, GL_RGBA, GL_HALF_FLOAT,
                        rgba)
        glBindFramebuffer(GL_FRAMEBUFFER, self.fbo)
        glViewport(0, 0, self.w, self.h)
        glUseProgram(self.prog); glBindVertexArray(self.vao)
        glDrawArrays(GL_TRIANGLES, 0, 3)
        # ponytail: glReadPixels to host costs ~19ms at 4K and dominates the
        # chain (upload 2.9, ACES shader 0.4). Reading RGB is 8x faster but the
        # CPU pad back to 4 channels gives it all back, so there is no win on
        # this side of the bus. The fix is GL->CUDA interop feeding NVENC a
        # device pointer and never touching host memory; worth doing when 4K
        # playback needs headroom, unnecessary for the 200ms slider budget.
        # GL_BGRA readback of an RGBA framebuffer lands as B,G,R,A in memory,
        # which is exactly NVENC's ARGB. Do NOT also swizzle in the shader --
        # the two cancel and you get RGBA back.
        buf = glReadPixels(0, 0, self.w, self.h, GL_BGRA, GL_UNSIGNED_BYTE)
        return np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 4)

def _selftest():
    import OpenImageIO as oiio
    make_context()
    inp = oiio.ImageInput.open("testdata/2k/test.0000.exr")
    lin = inp.read_image("half"); inp.close()
    h, w = lin.shape[:2]
    rgba = np.ascontiguousarray(np.dstack([lin, np.ones((h, w, 1), np.float16)]))
    g = OcioGL(w, h)
    print(f"shader: {g.desc.getShaderText().count(chr(10))} lines, "
          f"{len(list(g.desc.getTextures()))} 1D/2D luts, "
          f"{len(list(g.desc.get3DTextures()))} 3D luts")
    worst_frac = 0.0
    for ev in (-6.0, -2.0, 0.0, 2.0, 6.0):
        g.set_exposure(ev)
        got = g(rgba)[..., [2, 1, 0]].astype(np.float32)
        ref = np.ascontiguousarray(lin.reshape(-1, 3), np.float32)
        g.cpu.applyRGB(ref)
        ref = (np.clip(ref.reshape(h, w, 3), 0, 1) * 255.0 + 0.5).astype(np.uint8).astype(np.float32)
        err = np.abs(got - ref)
        bad = int((err.max(-1) > 2).sum())
        frac = bad / (h * w)
        worst_frac = max(worst_frac, frac)
        assert np.percentile(err, 99.9) <= 2.0, f"EV{ev}: p99.9 = {np.percentile(err,99.9)}"
        print(f"  EV{ev:+5.1f}  mean {err.mean():6.4f}  p99.9 {np.percentile(err,99.9):5.2f}"
              f"  max {err.max():5.1f}  bad>2: {bad} ({frac*100:.4f}%)")
    # OCIO's own CPU and GPU paths disagree on colours far outside the display
    # gamut -- e.g. AP1 green at 250x mid-grey is linear Rec.709
    # [-155.6, 285.9, -32.2]. CPU says white, GPU keeps hue. Physically
    # implausible content; 17 px in 2.2M here. Not our bug, and not worth
    # chasing -- but bound it so a real regression still trips the check.
    assert worst_frac < 5e-5, f"{worst_frac*100:.4f}% of pixels deviate; expected <0.005%"

    for _ in range(3): g(rgba)
    glFinish(); t = time.perf_counter()
    for _ in range(20): g(rgba)
    glFinish()
    dt = (time.perf_counter() - t) / 20
    print(f"  2K grade+readback {dt*1e3:5.2f} ms ({1/dt:5.0f} fps)")
    print("OK")

if __name__ == "__main__":
    _selftest()
