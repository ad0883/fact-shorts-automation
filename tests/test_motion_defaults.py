"""Default-motion regressions: real footage, text fallback honesty, and pacing."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from config import settings
from pipeline.editorial_motion import editorial_frame, memory_frame
from pipeline.run_store import read_json
from pipeline.storyboard import pace_shots, subject_storyboard, time_storyboard
from pipeline import stock_video
from test_content import Response


class DefaultMotionTests(unittest.TestCase):
    def test_subject_director_preserves_narration_and_keeps_hook_unrevealed(self):
        text="Which items do you remember? The first and last items in a list are often recalled better. This is the serial position effect. Earlier items may receive more rehearsal."
        beats=[{"role":"hook" if i==0 else "explain", "narration":s,
                "visual":{"kind":"question", "headline":"Memory", "labels":[]}}
               for i,s in enumerate(text.split("? "))]
        beats[0]["narration"]+="?"
        result=subject_storyboard(beats,text)
        self.assertEqual(" ".join(b["narration"] for b in result),text)
        self.assertTrue(all(b["visual"]["template"]=="memory_list" for b in result))
        self.assertEqual(result[0]["visual"]["view"],"question")
        unrelated="Octopuses are animals living in the ocean."
        self.assertEqual(subject_storyboard(beats,unrelated),beats)

    def test_default_graphics_and_subject_objects_change_without_caption_motion(self):
        import numpy as np
        for kind,labels in [("question",[]),("comparison",["First items","Last items"]),
                            ("sequence",["First","Second","Third"])]:
            beat={"role":"explain","visual":{"kind":kind,"headline":"Look closer", "labels":labels}}
            a=np.asarray(editorial_frame(beat,.1,3,360,640))
            b=np.asarray(editorial_frame(beat,2.1,3,360,640))
            self.assertGreater(np.mean(np.any(a[180:440]!=b[180:440],axis=2)),.005)
        a=np.asarray(memory_frame("middle",.1,3,360,640,"The middle"))
        b=np.asarray(memory_frame("middle",2.1,3,360,640,"The middle"))
        self.assertGreater(np.mean(np.any(a[180:440]!=b[180:440],axis=2)),.005)

    def test_long_photos_reframe_and_video_shots_advance_source(self):
        scenes=pace_shots([{"start":0,"end":11.58,"image_path":"photo.jpg","fit":"cover"},
                           {"start":11.58,"end":20,"image_path":"clip.mp4","media_type":"video"}])
        self.assertTrue(all(s["end"]-s["start"]<=3.5 for s in scenes))
        self.assertEqual(scenes[0]["start"],0)
        self.assertEqual(scenes[-1]["end"],20)
        for a,b in zip(scenes,scenes[1:]): self.assertEqual(a["end"],b["start"])
        footage=[s for s in scenes if s.get("media_type")=="video"]
        self.assertEqual(footage[0]["source_offset"],0)
        self.assertGreater(footage[-1]["source_offset"],0)
        self.assertGreater(len({s["camera"] for s in scenes}),1)

    def test_default_graphic_encodes_moving_media(self):
        from pipeline.story_graphics import render_graphic
        beat={"start":0,"end":.5,"beat_index":0,"role":"explain",
              "visual":{"kind":"sequence","headline":"Follow the process","labels":["Start","Finish"]}}
        with tempfile.TemporaryDirectory() as tmp, patch.multiple(settings,VIDEO_WIDTH=360,VIDEO_HEIGHT=640,VIDEO_FPS=12):
            scene=render_graphic(beat,Path(tmp))[0]
            self.assertEqual(scene["media_type"],"video")
            self.assertTrue(scene["text_only"])
            result=subprocess.run(["ffmpeg","-v","error","-i",scene["image_path"],"-f","framemd5","-"],capture_output=True,text=True,check=True)
            hashes=[l.rsplit(",",1)[-1] for l in result.stdout.splitlines() if l and not l.startswith("#")]
            self.assertGreater(len(set(hashes)),3)


class FootageTests(unittest.TestCase):
    def test_attached_foam_is_not_used_to_illustrate_a_free_sphere(self):
        self.assertFalse(stock_video.context_matches("A free soap bubble tends toward a sphere", "soap bubbles on glass surface"))
        self.assertTrue(stock_video.context_matches("Giant soap bubbles can distort", "giant soap bubbles in the street"))

    def candidate(self,asset="https://videos.pexels.com/video-files/1/clip.mp4"):
        return {"videos":[{"id":1,"url":"https://www.pexels.com/video/writing-notebook-1/",
                           "duration":10,"user":{"name":"Creator"},"video_files":[
                               {"file_type":"video/mp4","width":1280,"height":720,"link":asset}]}]}

    @patch.dict(os.environ,{"PEXELS_API_KEY":"test"})
    @patch("pipeline.stock_video.probe_media",return_value={"streams":[{"codec_type":"video"}],"format":{"duration":"10"}})
    @patch("pipeline.stock_video.requests.get")
    def test_footage_is_attributed_and_auth_never_goes_to_asset_host(self,get,probe):
        get.side_effect=[Response(self.candidate()),Response(body=b"mp4",headers={"Content-Type":"video/mp4","Content-Length":"3"})]
        with tempfile.TemporaryDirectory() as tmp:
            meta={};p=Path(tmp)/"clip.mp4"
            self.assertTrue(stock_video.download_pexels_video("writing notebook",p,metadata=meta))
            self.assertEqual(meta["usage"],"illustrative_stock_video")
            self.assertEqual(meta["photographer"],"Creator")
            self.assertNotIn("headers",get.call_args_list[1].kwargs)
            self.assertFalse(get.call_args_list[1].kwargs["allow_redirects"])

    @patch.dict(os.environ,{"PEXELS_API_KEY":"test"})
    @patch("pipeline.stock_video.requests.get")
    def test_irrelevant_or_untrusted_footage_never_downloads(self,get):
        for query,asset in [("ocean octopus","https://videos.pexels.com/video-files/1/clip.mp4"),
                            ("writing notebook","https://videos.pexels.com.evil.test/video-files/1/clip.mp4")]:
            get.reset_mock();get.return_value=Response(self.candidate(asset))
            self.assertFalse(stock_video.download_pexels_video(query,Path("unused.mp4")))
            get.assert_called_once()

    @patch.dict(os.environ,{"PEXELS_API_KEY":"test"})
    @patch("pipeline.stock_video.requests.get")
    def test_partial_download_preserves_previous_asset(self,get):
        get.side_effect=[Response(self.candidate()),Response(body=b"partial",headers={"Content-Type":"video/mp4","Content-Length":"100"})]
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"clip.mp4";p.write_bytes(b"original")
            self.assertFalse(stock_video.download_pexels_video("writing notebook",p))
            self.assertEqual(p.read_bytes(),b"original")
            self.assertEqual(list(Path(tmp).glob(".stock-*")),[])


if __name__=="__main__":
    unittest.main()
