"""Subject-specific, animated explanatory drawings. These are not CFD simulations."""

from functools import lru_cache
import math
from pathlib import Path
import subprocess
import tempfile

from PIL import Image, ImageDraw

from config import settings
from pipeline.story_graphics import _font, _text


VIEWS = {"question", "overview", "smooth", "drag", "dimples", "attached", "wake", "comparison"}
INK = "#091622"
WHITE = "#f3f6ee"
MINT = "#6be5c3"
CORAL = "#ff967e"
GOLD = "#f1d172"


@lru_cache(maxsize=96)
def _ball(radius, dimpled, rotation=0):
    """A shaded sphere with dimples projected onto its visible hemisphere."""
    r = radius
    sprite = Image.new("RGBA", (2*r+6, 2*r+6))
    draw = ImageDraw.Draw(sprite)
    cx, cy = r+3, r+3
    draw.ellipse((3,3,2*r+3,2*r+3), fill="#798d9a")
    for i in range(80, 0, -1):
        a = i/80
        rr = r*a
        x, y = cx-r*.22*(1-a), cy-r*.25*(1-a)
        shade = int(248-94*a*a)
        draw.ellipse((x-rr,y-rr,x+rr,y+rr), fill=(shade,min(255,shade+5),min(255,shade+7)))
    if dimpled:
        for row in range(-7,8):
            lat = row*math.pi/17
            for col in range(28):
                lon = col*2*math.pi/28+rotation+(row%2)*math.pi/28
                depth = math.cos(lat)*math.cos(lon)
                if depth < .12:
                    continue
                x = cx+math.cos(lat)*math.sin(lon)*r*.96
                y = cy+math.sin(lat)*r*.96
                ry = r*.049
                rx = ry*max(.26,depth)
                base = sprite.getpixel((round(x),round(y)))[:3]
                shade = tuple(max(0,v-23) for v in base)+(255,)
                highlight = tuple(min(255,v+20) for v in base)+(255,)
                draw.ellipse((x-rx,y-ry,x+rx,y+ry), fill=shade)
                draw.arc((x-rx,y-ry,x+rx,y+ry), 15,160,fill=highlight,width=max(1,r//90))
    return sprite


@lru_cache(maxsize=8)
def _backdrop(w,h):
    image = Image.new("RGB", (w,h), INK)
    d = ImageDraw.Draw(image)
    for y in range(h):
        glow = max(0,1-abs(y/h-.44)*1.5)
        d.line((0,y,w,y), fill=(8+int(glow*5),20+int(glow*12),31+int(glow*13)))
    step = max(12,round(w/12))
    for x in range(step,w,step):
        d.line((x,round(h*.25),x,round(h*.72)), fill="#162f3b",width=1)
    for y in range(round(h*.25),round(h*.72),step):
        d.line((round(w*.035),y,round(w*.965),y),fill="#162f3b",width=1)
    return image


def _center_text(draw, text, w, top, bottom, size, color=WHITE):
    _text(draw,text,(w*.07,top,w*.89,bottom),int(size),color)


def _arrow(draw, start, end, color, width):
    draw.line((*start,*end),fill=color,width=width)
    angle = math.atan2(end[1]-start[1],end[0]-start[0])
    size = max(5,width*3)
    draw.polygon([end,(end[0]-size*math.cos(angle-.5),end[1]-size*math.sin(angle-.5)),
                  (end[0]-size*math.cos(angle+.5),end[1]-size*math.sin(angle+.5))],fill=color)


def _curve(points, samples=35):
    result=[]
    for j in range(samples):
        t=j/(samples-1)
        a,b,c,d=points
        result.append(((1-t)**3*a[0]+3*(1-t)**2*t*b[0]+3*(1-t)*t*t*c[0]+t**3*d[0],
                       (1-t)**3*a[1]+3*(1-t)**2*t*b[1]+3*(1-t)*t*t*c[1]+t**3*d[1]))
    return result


def _flow(image, cx, cy, r, t, attachment, *, show_drag=False, label=None, dimpled=None):
    """Qualitative streamlines and wakes; no scales or fabricated measurements."""
    w,h=image.size
    draw=ImageDraw.Draw(image)
    a=attachment
    separation=.22+.58*a
    spread=.95-.53*a
    color=MINT if a>.5 else CORAL
    right=w*.94
    wake=Image.new("RGBA",image.size)
    wd=ImageDraw.Draw(wake)
    x0=cx+separation*r
    upper=_curve([(x0,cy-r*spread),(cx+1.6*r,cy-r*(spread+.08)),(right-r,cy-r*spread*.8),(right,cy-r*spread)])
    lower=[(x,2*cy-y) for x,y in upper]
    wd.polygon(upper+list(reversed(lower)), fill=(74,212,181,36) if a>.5 else (255,131,104,42))
    for j in range(9):
        phase=(t*.32+j/9)%1
        x=x0+(right-x0)*phase
        y=cy+math.sin(j*3.7+t*2)*r*spread*.58
        rr=r*(.09+.10*(1-a))*(.8+.4*math.sin(j+t))
        wd.arc((x-rr,y-rr,x+rr,y+rr),j*31+t*120,j*31+t*120+245,
               fill=(95,225,195,120) if a>.5 else (255,150,120,150),width=max(1,round(w*.003)))
    image.paste(wake,(0,0),wake)
    draw=ImageDraw.Draw(image)
    for sign in (-1,1):
        for lane in range(4):
            offset=.22+lane*.34
            peak=1.07+lane*.20
            tail=spread+lane*.34
            pts=_curve([(w*.035,cy+sign*offset*r),(cx-1.5*r,cy+sign*offset*r),
                        (cx-1.3*r,cy+sign*peak*r),(cx,cy+sign*peak*r)])
            pts+=_curve([(cx,cy+sign*peak*r),(cx+(.28+.38*a)*r,cy+sign*peak*r),
                         (cx+(.9+.45*a)*r,cy+sign*tail*r),(right,cy+sign*tail*r)])[1:]
            draw.line(pts,fill="#2d6b73",width=max(1,round(w*.002)))
            for particle in range(3):
                pos=int(((t*.48+particle/3+lane*.08)%1)*(len(pts)-5))
                draw.line(pts[pos:pos+5],fill=MINT,width=max(2,round(w*.004)))
    rotation=round(t*.35,1)
    is_dimpled = a>.5 if dimpled is None else dimpled
    ball=_ball(round(r), is_dimpled, rotation if is_dimpled else 0)
    image.paste(ball,(round(cx-r-3),round(cy-r-3)),ball)
    if show_drag:
        # The drawing's relative airflow is left -> right, so drag points right.
        y=cy+r*1.72
        _arrow(draw,(cx-r*.6,y),(cx+r*1.45,y),GOLD,max(2,round(w*.006)))
        draw.text((cx+r*.4,y+r*.18),"DRAG",font=_font(max(8,round(w*.033))),fill=GOLD,anchor="mt")
    if label:
        draw.text((w*.07,cy-r*2.0),label,font=_font(max(9,round(w*.036))),fill=color,anchor="lt")


def golf_frame(view, t, duration, w, h, headline):
    image=_backdrop(w,h).copy()
    draw=ImageDraw.Draw(image)
    # All scene types share the same quiet edge, making the subject continuous.
    draw.rounded_rectangle((w*.07,h*.064,w*.18,h*.069),radius=3,fill=MINT)
    draw.text((w*.07,h*.085),"THE SCIENCE OF FLIGHT",font=_font(max(8,round(w*.026))),fill=MINT)
    _center_text(draw,headline,w,h*.125,h*.235,w*.066)
    progress=min(1,t/max(.1,duration))
    if view == "question":
        r=round(w*.165)
        for i,dimpled in enumerate((False,True)):
            cx=w*(.27 if i==0 else .71)
            cy=h*.435+math.sin(t*2+i)*w*.012
            glow=Image.new("RGBA",image.size)
            gd=ImageDraw.Draw(glow)
            gd.ellipse((cx-r*1.28,cy-r*1.28,cx+r*1.28,cy+r*1.28),outline=(107,229,195,65),width=max(1,w//180))
            image.paste(glow,(0,0),glow)
            ball=_ball(r,dimpled,round(t*.35,1) if dimpled else 0)
            image.paste(ball,(round(cx-r-3),round(cy-r-3)),ball)
        draw=ImageDraw.Draw(image)
        for x,label in ((.27,"SMOOTH"),(.71,"DIMPLED")):
            draw.text((w*x,h*.57),label,font=_font(round(w*.042)),fill=WHITE,anchor="mt")
        _center_text(draw,"Make your pick.",w,h*.635,h*.68,w*.042,MINT)
    elif view in {"comparison","overview"}:
        for i,a in enumerate((0,1)):
            _flow(image,w*.30,h*(.36 if i==0 else .59),w*.12,t,a,
                  label="SMOOTH" if i==0 else "DIMPLED")
        draw=ImageDraw.Draw(image)
        if view == "comparison":
            draw.text((w*.60,h*.42),"BIGGER WAKE",font=_font(round(w*.029)),fill=CORAL,anchor="mt")
            draw.text((w*.60,h*.65),"LESS DRAG",font=_font(round(w*.034)),fill=MINT,anchor="mt")
    elif view == "dimples":
        r=round(w*.28);cx=w*.48;cy=h*.435
        for j in range(28):
            angle=j*2*math.pi/28+t*.65
            rr=r*(1.06+.014*math.sin(t*8+j))
            x,y=cx+math.cos(angle)*rr,cy+math.sin(angle)*rr
            draw.ellipse((x-w*.004,y-w*.004,x+w*.004,y+w*.004),fill=MINT)
        ball=_ball(r,True,round(t*.35,1))
        image.paste(ball,(round(cx-r-3),round(cy-r-3)),ball)
        draw=ImageDraw.Draw(image)
        _center_text(draw,"A thin layer of air",w,h*.635,h*.685,w*.045,MINT)
    else:
        attachment=1 if view == "attached" else min(1,max(0,(progress-.10)/.68)) if view=="wake" else 0
        _flow(image,w*.34,h*.455,w*.19,t,attachment,show_drag=view=="drag",dimpled=True if view=="wake" else None)
        draw=ImageDraw.Draw(image)
        draw.text((w*.07,h*.278),"RELATIVE AIRFLOW",font=_font(round(w*.025)),fill="#93b2bf")
        _arrow(draw,(w*.50,h*.29),(w*.73,h*.29),MINT,max(2,round(w*.003)))
        callout={"smooth":"Air separates early", "attached":"Air follows the curve longer", "wake":"The wake shrinks"}.get(view)
        if callout:
            _center_text(draw,callout,w,h*.64,h*.69,w*.043,MINT if attachment>.5 else CORAL)
    draw=ImageDraw.Draw(image)
    draw.text((w*.07,h*.717),"ILLUSTRATION · NOT TO SCALE",font=_font(max(7,round(w*.021))),fill="#7c9aa8")
    # Captions occupy the area around 78% height, clear of the illustration.
    return image


def encode_motion(beat, directory, frame_function):
    """Encode frame callbacks atomically, streaming rather than storing a movie in RAM."""
    visual=beat["visual"]
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    w,h=settings.VIDEO_WIDTH,settings.VIDEO_HEIGHT
    fps=settings.VIDEO_FPS
    duration=beat["end"]-beat["start"]
    count=math.ceil(duration*fps)+2
    path=directory/f"motion_{beat['beat_index']:02d}_{round(beat['start']*1000):06d}.mp4"
    with tempfile.TemporaryDirectory(prefix=".motion-",dir=directory) as tmp:
        output=Path(tmp)/"animation.mp4"
        # Raw frames are streamed, keeping memory bounded even for 1080x1920.
        command=["ffmpeg","-v","error","-y","-f","rawvideo","-pix_fmt","rgb24","-s",f"{w}x{h}",
                 "-r",str(fps),"-i","pipe:0","-an","-c:v","libx264","-preset","fast","-crf","18",
                 "-pix_fmt","yuv420p","-movflags","+faststart",str(output)]
        with tempfile.TemporaryFile() as errors:
            process=subprocess.Popen(command,stdin=subprocess.PIPE,stderr=errors,stdout=subprocess.DEVNULL)
            try:
                for frame in range(count):
                    picture=frame_function(frame/fps,duration,w,h)
                    process.stdin.write(picture.tobytes())
                process.stdin.close()
                if process.wait(timeout=120):
                    errors.seek(0)
                    raise RuntimeError("Animation encoding failed: "+errors.read().decode(errors="replace")[-2000:])
                output.replace(path)
            except BaseException:
                process.kill();process.wait()
                raise
    return [{"image_path":str(path.resolve()),"media_type":"video","start":beat["start"],"end":beat["end"],
             "layout":"full","fit":"contain","kind":"animation","headline":"","role":beat["role"],
             "beat_index":beat["beat_index"],"template":visual.get("template", "editorial"),"view":visual.get("view", visual.get("kind", "animation")),
             "fallback":bool(beat.get("fallback")), "visual_basis":beat.get("visual_basis", "planned"),
             "provenance":{"provider":"local","usage":"explanatory_animation","description":visual["headline"]}}]


def render_animation(beat, directory):
    visual = beat["visual"]
    if visual.get("template") == "memory_list":
        from pipeline.editorial_motion import memory_frame
        if visual.get("view") not in {"question", "overview", "primacy", "recency", "middle", "comparison"}:
            raise ValueError("Unsupported memory animation view")
        return encode_motion(beat, directory, lambda t,d,w,h: memory_frame(visual["view"], t,d,w,h,visual["headline"]))
    if visual.get("template") != "golf_airflow" or visual.get("view") not in VIEWS:
        raise ValueError("Unsupported animation template or view")
    return encode_motion(beat, directory, lambda t,d,w,h: golf_frame(visual["view"], t,d,w,h,visual["headline"]))
