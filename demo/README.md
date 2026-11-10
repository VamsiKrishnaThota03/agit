# The agit demo kit

Everything needed to record a 2–3 minute demo video with your own voice.

| file | what it is |
|---|---|
| `agit-demo.mp4` | **silent 1080p video, 2:54**, captions burned in at the bottom. Drop it into iMovie and narrate over it. |
| `narration.txt` | the script: per scene, the on-screen time, the caption, and the exact words to say — timed so reading at a normal pace fits the scene. |
| `captions.srt` | the same captions as a subtitle file (for YouTube, Final Cut, DaVinci — iMovie does not import SRT). |
| `frames/` | all 117 PNG frames, if you want stills for slides or a thumbnail. Regenerated, not committed. |

Nothing in the video is staged. `tools/make_demo_video.py` creates a throwaway
workspace, runs the real `agit` commands against the bundled offline planner,
captures their real output and animates it. Re-run it after any change:

```bash
python3 tools/make_demo_video.py          # ~3 minutes, needs ffmpeg + Chrome
```

---

## Option A — narrate over the video (easiest, recommended)

1. Open **iMovie → Create New → Movie**, then drag in `agit-demo.mp4`.
2. Select the clip, press **V** to record a voiceover (or iMovie's microphone
   button), and read from `narration.txt`. Each scene's on-screen time is
   already matched to its word count, so you can read straight through at a
   calm pace.
3. If you fall behind on a scene, split the clip at that boundary
   (**⌘B**) and stretch it — the frames are static, so stretching looks fine.
4. Export: **File → Share → File → 1080p, High quality**.

The timings, for cueing:

| # | start | caption on screen |
|---|---|---|
| 1 | 0:00 | An AI wrote this line. Why? Nobody can answer that today. |
| 2 | 0:10 | agit writes every agent step into a git ledger: one step, one commit. |
| 3 | 0:22 | The agent writes a failing test first, then the fix — and each step is a commit. |
| 4 | 0:41 | VERIFIED is not the model's opinion — agit ran your tests before believing it. |
| 5 | 0:53 | Human lines and agent lines, separated line by line. |
| 6 | 1:04 | Point at any line and ask why. The answer was stored when the line was written. |
| 7 | 1:18 | Rewind to step 4 and try a different approach. The ledger branches like git. |
| 8 | 1:30 | Two verified solutions to the same task, side by side. |
| 9 | 1:40 | Diff the outcomes, then keep the one you prefer. |
| 10 | 1:50 | Every model call is recorded, so a run replays byte-identically — for free. |
| 11 | 2:04 | The dashboard: every run, every step, every reason — one static HTML file. |
| 12 | 2:19 | Click any line: the step and the reasoning pin to the top. |
| 13 | 2:33 | And where the tokens actually went, step by step. |
| 14 | 2:42 | Zero dependencies, 97 offline tests, and a demo that needs no API key. |

## Option B — record your own screen live

Use this if you would rather show a real terminal and your real cursor.

```bash
./demo/run-demo.sh            # pauses before and after every command
./demo/run-demo.sh --auto     # runs itself with pauses, for a hands-off take
```

It builds a clean workspace in `~/agit-screencast`, clears the screen between
scenes, types each command out, and waits for you to press **Enter**. So:

1. Start **QuickTime Player → File → New Screen Recording** (or ⇧⌘5), record
   the terminal window only, with microphone on if you want one take.
2. Run the script, press Enter at your own pace, and read the matching scene
   from `narration.txt`.
3. For the dashboard part at the end, the script opens the generated HTML file
   in your browser; click the **blame** tab and click line 24 on camera — that
   single click is the most convincing moment in the whole demo.

Tips that make a visible difference:

- Terminal at ~92 columns, font around 16–18 pt, dark theme.
- Hide your dock, menu-bar clutter and notifications (Do Not Disturb).
- Record at 1080p or better; avoid resizing the window mid-take.

## Option C — 60-second cut

If a submission limits you to a minute, keep scenes **1, 3, 5, 6, 12** and the
closing card. That is the whole argument: the problem, the agent working, who
wrote which line, why that line exists, the UI, and where to get it.
