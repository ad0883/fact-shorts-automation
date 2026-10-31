"""Moving, narration-led typography and illustrative subject demonstrations."""

from functools import lru_cache
import math

from PIL import Image, ImageDraw

from pipeline.story_graphics import _font, _text

NAVY = "#101c30"
CREAM = "#f7f0df"
CORAL = "#fa755d"
BLUE = "#5287f4"
MINT = "#7bdfba"


def ease(value):
    value = max(0, min(1, value))
    return 1-(1-value)**3


@lru_cache(maxsize=8)
def backdrop(w, h, light=False):
    im = Image.new("RGB", (w,h), CREAM if light else NAVY)
    d = ImageDraw.Draw(im)
    # Broad, quiet shapes provide depth without suggesting any scientific data.
    d.ellipse((w*.55,-w*.6,w*1.7,w*.55), fill="#ebe2cf" if light else "#1b304d")
    d.ellipse((-w*.7,h*.5,w*.23,h*.5+w*.93), fill="#efe6d5" if light else "#152740")
    return im


def header(draw, headline, w,h, light=False, tag=""):
    ink = NAVY if light else CREAM
    if tag:
        draw.text((w*.08,h*.075),tag.upper(),font=_font(max(9,round(w*.026))),fill=CORAL)
    _text(draw,headline,(w*.08,h*.115,w*.89,h*.245),round(w*.071),ink)


def editorial_frame(beat, t, duration, w,h):
    visual=beat["visual"]
    kind=visual["kind"]
    light=kind=="comparison"
    im=backdrop(w,h,light).copy();d=ImageDraw.Draw(im)
    ink=NAVY if light else CREAM
    labels=visual.get("labels",[])
    if not labels:
        # A short thought fills the frame; it never becomes a paragraph card.
        mark="?" if kind=="question" and beat["role"]=="hook" and not beat.get("fallback") else "•"
        d.text((w*.49,h*.27+math.sin(t*2)*h*.008),mark,font=_font(round(w*.29)),fill=CORAL,anchor="mm")
        offset=h*.018*(1-ease(t/.38))
        _text(d,visual["headline"],(w*.09,h*.38+offset,w*.88,h*.64+offset),round(w*.098),ink)
        line_w=w*(.10+.50*ease(t/max(.2,duration*.7)))
        d.line((w*.09,h*.69,w*.09+line_w,h*.69),fill=MINT,width=max(2,round(w*.008)))
        return im
    header(d,visual["headline"],w,h,light, "" if kind=="comparison" else "STEP BY STEP")
    phase=min(len(labels)-1,int(t/max(.1,duration)*len(labels)))
    if kind=="comparison":
        # Two editorial lanes, not arbitrary data bars or fabricated quantities.
        d.line((w*.49,h*.31,w*.49,h*.66),fill="#d8cfbd",width=max(1,w//200))
        for i,label in enumerate(labels):
            x=w*(.27 if i==0 else .71)
            color=CORAL if i==0 else BLUE
            focus=1 if phase==i else .88
            radius=w*.11*focus
            y=h*.38+math.sin(t*1.8+i)*h*.005
            d.ellipse((x-radius,y-radius,x+radius,y+radius),fill=color)
            d.text((x,y),"A" if i==0 else "B",font=_font(round(w*.11*focus)),fill=CREAM,anchor="mm")
            _text(d,label,(x-w*.19,h*.49,x+w*.19,h*.66),round(w*.052),ink)
        d.text((w*.49,h*.36),"/",font=_font(round(w*.07)),fill=NAVY,anchor="mm")
    else:
        # A moving marker follows the actual ordered steps. Labels stay readable.
        count=len(labels);ys=[h*(.33+i*.30/(count-1)) for i in range(count)]
        x=w*.18
        d.line((x,ys[0],x,ys[-1]),fill="#34556d",width=max(2,round(w*.008)))
        p=min(.999,t/max(.1,duration))*(count-1)
        dot_y=ys[0]+(ys[-1]-ys[0])*(p/(count-1))
        d.ellipse((x-w*.014,dot_y-w*.014,x+w*.014,dot_y+w*.014),fill=CORAL)
        for i,(label,y) in enumerate(zip(labels,ys)):
            r=w*(.055 if phase==i else .044)
            d.ellipse((x-r,y-r,x+r,y+r),fill=MINT if phase==i else "#29455b")
            d.text((x,y),str(i+1),font=_font(round(w*.04)),fill=NAVY if phase==i else CREAM,anchor="mm")
            _text(d,label,(w*.30,y-h*.055,w*.88,y+h*.055),round(w*.054),CREAM if phase==i else "#9eb1c2")
    return im


def render_editorial(beat,directory):
    from pipeline.motion_graphics import encode_motion
    scenes=encode_motion(beat,directory,lambda t,d,w,h: editorial_frame(beat,t,d,w,h))
    for scene in scenes:
        scene.update(kind=beat["visual"]["kind"], text_only=True)
    return scenes


def _object(draw,kind,x,y,r,color):
    """Recognizable objects make the example list something to look at."""
    if kind=="APPLE":
        draw.ellipse((x-r,y-r*.6,x+r*.2,y+r),fill=color)
        draw.ellipse((x-r*.2,y-r*.6,x+r,y+r),fill=color)
        draw.line((x,y-r*.45,x+r*.12,y-r),fill=NAVY,width=max(2,round(r*.16)))
        draw.ellipse((x+r*.05,y-r*1.05,x+r*.65,y-r*.63),fill="#368565")
    elif kind=="KEY":
        draw.ellipse((x-r,y-r*.75,x,y+r*.25),outline=color,width=max(2,round(r*.25)))
        draw.line((x-r*.2,y,x+r,y+r*.9),fill=color,width=max(2,round(r*.24)))
        draw.line((x+r*.6,y+r*.6,x+r*.85,y+r*.3),fill=color,width=max(2,round(r*.24)))
    elif kind=="MOON":
        draw.ellipse((x-r,y-r,x+r,y+r),fill=color)
        draw.ellipse((x-r*.2,y-r*1.1,x+r*1.2,y+r*.5),fill=CREAM)
    elif kind=="LEAF":
        draw.ellipse((x-r*.8,y-r,x+r*.8,y+r),fill=color)
        draw.line((x-r*.4,y+r*.75,x+r*.4,y-r*.75),fill=CREAM,width=max(1,round(r*.10)))
    elif kind=="STAR":
        points=[(x+math.cos(-math.pi/2+i*math.pi/5)*r*(1 if i%2==0 else .43),
                 y+math.sin(-math.pi/2+i*math.pi/5)*r*(1 if i%2==0 else .43)) for i in range(10)]
        draw.polygon(points,fill=color)
    else:
        draw.ellipse((x-r,y-r*.6,x+r*.6,y+r*.6),fill=color)
        draw.polygon([(x+r*.4,y),(x+r,y-r*.7),(x+r,y+r*.7)],fill=color)
        draw.ellipse((x-r*.65,y-r*.15,x-r*.45,y+r*.05),fill=CREAM)


def memory_frame(view,t,duration,w,h,headline):
    im=backdrop(w,h).copy();d=ImageDraw.Draw(im)
    header(d,headline,w,h,tag="MEMORY / SERIAL POSITION")
    names=["APPLE","KEY","MOON","LEAF","STAR","FISH"]
    colors=[CORAL,"#dba83b",BLUE,"#328e6c","#bd7ccc","#398fba"]
    if view in {"question", "primacy", "recency", "middle"}:
        # Cut from the whole list to the actual objects at the relevant positions.
        # This changes the composition as the explanation advances.
        selected = ([min(5,int(t/max(.2,duration)*6))] if view=="question" else
                    [0,1] if view=="primacy" else [4,5] if view=="recency" else [2,3])
        for j,i in enumerate(selected):
            x=w*.49 if len(selected)==1 else w*(.28 if j==0 else .70)
            y=h*.435+math.sin(t*2+j)*h*.007
            r=w*(.20 if len(selected)==1 else .13)
            d.ellipse((x-r*1.26,y-r*1.26,x+r*1.26,y+r*1.26),fill=CREAM)
            # Draw on the main frame so moons share the cream disc behind them.
            _object(d,names[i],x,y,r*.75,colors[i])
            if view=="middle":
                # Fade the example objects qualitatively; never display a score.
                crop=im.crop((int(x-r*1.22),int(y-r*1.22),int(x+r*1.22),int(y+r*1.22)))
                fade=Image.new("RGB",crop.size,CREAM)
                crop=Image.blend(crop,fade,.75*ease(t/max(.2,duration*.8)))
                mask=Image.new("L",crop.size,0)
                ImageDraw.Draw(mask).ellipse((0,0,crop.width-1,crop.height-1),fill=255)
                im.paste(crop,(int(x-r*1.22),int(y-r*1.22)),mask)
                d=ImageDraw.Draw(im)
            d.text((x,h*.575),names[i],font=_font(round(w*.055)),fill=CREAM,anchor="mt")
        # A six-position strip preserves the relationship to the original list.
        for i in range(6):
            x=w*(.16+i*.13);y=h*.66;r=w*.027
            d.ellipse((x-r,y-r,x+r,y+r),fill=CORAL if i in selected else "#31445e")
            d.text((x,y),str(i+1),font=_font(round(w*.025)),fill=CREAM,anchor="mm")
        d.text((w*.12,h*.718),"EXAMPLE LIST · ILLUSTRATION",font=_font(max(8,round(w*.023))),fill="#a9bdcb")
        return im
    # Paper list at an angle-free, readable scale; emphasis shifts within it.
    left,right=w*.12,w*.86
    top,bottom=h*.275,h*.685
    d.rounded_rectangle((left+5,top+8,right+5,bottom+8),radius=w*.035,fill="#081326")
    d.rounded_rectangle((left,top,right,bottom),radius=w*.035,fill=CREAM)
    step=(bottom-top)/6
    for i,(name,color) in enumerate(zip(names,colors)):
        y=top+step*(i+.5)
        if view=="question":
            active=i==min(5,int(t/max(.2,duration*.75)*6))
        elif view=="primacy":
            active=i<2
        elif view=="recency":
            active=i>3
        elif view=="middle":
            active=2<=i<=3
        else:
            active=i<2 or i>3
        color_fill="#f2d2bd" if view=="middle" else "#dceada"
        if active:
            inset=w*.016*(1-ease(t/.45))
            d.rounded_rectangle((left+w*.018+inset,y-step*.43,right-w*.018,y+step*.43),radius=w*.015,fill=color_fill)
        drift=math.sin(t*2+i*.5)*w*.004 if active else 0
        _object(d,name,left+w*.12+drift,y,step*.29,color)
        textcolor=NAVY
        if view=="middle" and 2<=i<=3:
            # Qualitative fading, not a measured probability or a viewer test result.
            q=ease(t/max(.2,duration*.75))
            textcolor=tuple(round(a+(b-a)*q) for a,b in zip((16,28,48),(171,157,145)))
        d.text((left+w*.23+drift,y),name,font=_font(round(w*.046)),fill=textcolor,anchor="lm")
        if active:
            x=right-w*.055
            r=w*(.008+.002*math.sin(t*3+i))
            d.ellipse((x-r,y-r,x+r,y+r),fill=CORAL if view=="middle" else "#368565")
    d.text((w*.12,h*.718),"EXAMPLE LIST · ILLUSTRATION",font=_font(max(8,round(w*.023))),fill="#a9bdcb")
    return im
