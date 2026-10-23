# iRacing Replay Director

Turns an iRacing replay into a highlights video with AI commentary. Everything runs on your own PC, with no paid services.

**How it works**

1. **scan**: plays the replay at 16x through the iRacing SDK and logs every car's position on track (about 2 minutes for a 30-minute race). It also collects iRacing's own incident markers.
2. **plan**: finds the start, overtakes, battles, spins, crashes and the finish, scores each one, and builds an edit list that fits your target length. Your own car gets extra screen time.
3. **capture**: jumps the replay to each moment, picks the car and camera (TV1/TV2/Chase/Chopper...), cuts between angles, and records each clip with OBS.
4. **commentary**: a local LLM (Ollama) writes a line for each moment, using only the real race data (names, positions, laps, gaps).
5. **voice**: Kokoro text-to-speech reads the lines (the default voice is British, `bm_george`).
6. **render**: ffmpeg adds a title card, lower-third captions, commentary over the game audio (the game audio dips while the commentator talks) and a results card.

## The dashboard (start here)

Double-click **`dashboard.bat`**. A control panel opens in your browser. The only things you do by hand are open the replay in iRacing and start OBS. Everything else happens from the panel:
- **Status lights:** whether iRacing has a replay open, whether OBS is reachable, whether Ollama and its model are ready, and whether Chatterbox is installed.
- **Race setup:** right after each scan, a page lists the drivers in that race. Pick the featured driver, or **All drivers** to treat everyone equally, or type a car number or name. Then set the video length.
- **Run:** the full highlights video, a Race in 30, a rebuild from the last scan, or any single step. You see a live log, the current step and the elapsed time, plus Stop and Press Enter buttons. When the review page is waiting for you, a link appears.
- **Videos:** watch every finished video in the page, or open its folder.
- **Script:** edit any commentary line, then use "Save, re-voice and rebuild".
- **Settings:** every option as a form. Camera lists come with clickable chips for the cameras this track has, and you can edit name pronunciations here too. Changes go straight into `config.toml`, and only the settings you change are touched.

Keep the black dashboard window open while you use it.

## One-time setup

1. **Python 3.11+**: install from python.org. During install, tick *Add python.exe to PATH*.
2. **OBS Studio 30+**
   * Create a scene with a **Game Capture** source for iRacing, plus Desktop Audio for the game sound.
   * Settings > Video: base and output resolution 1920x1080, 60 FPS.
   * Settings > Output > Recording: format **Hybrid MP4** or MKV.
   * Tools > **WebSocket Server Settings**: tick *Enable WebSocket server*, then copy the password into the dashboard's Settings tab (or `config.toml` under `[capture]`).
3. **Double-click `setup.bat`.** It installs the Python packages, downloads the voice model (340 MB), and installs ffmpeg and Ollama if they're missing. It also pulls the `llama3.1:8b` model (4.7 GB). If it has to install Ollama first, open a new terminal afterwards and run `ollama pull llama3.1:8b`.
4. **Test without iRacing:** `run.bat fake`. This builds a full video from a made-up race with test-pattern footage, so you can hear the voice and check that everything is wired up.

## Making a video

1. Start OBS.
2. In iRacing, open your replay (Replays > Watch). Windowed or fullscreen both work. Leave it alone while it records, because the tool controls the replay.
3. Open a terminal in this folder and run:

```
run.bat all
```

The video is saved to `output\<track>-<subsession>\<name>_highlights.mp4`.

### Running steps one at a time

Every step saves its results, so you can re-run any step without redoing the earlier ones:

| Command | Needs | What to tweak |
|---|---|---|
| `run.bat scan` | iRacing with the replay open | - |
| `run.bat plan` | nothing | `[highlights]` in config.toml, then re-run |
| `run.bat capture` | iRacing + OBS | `--clips 3,7` re-records only those clips |
| `run.bat commentary` | Ollama | edit lines by hand in `script.json` |
| `run.bat voice` | nothing | `[tts]` voice/speed |
| `run.bat render` | nothing | `[render]` volumes, captions |

**Review page:** after the highlights are picked, a page opens in your browser. Untick clips, fix start/end times, change which car and camera each shot uses, edit captions, add a note for the commentator (treated as fact), or add moments the app missed. Run it any time with `run.bat review`; afterwards `run.bat capture` only re-records clips you changed. Turn it off with `review_before_capture = false` under `[highlights]` or `--no-review`.

`run.bat remake` runs every step after the scan, so you can change settings and rebuild without scanning again.

All steps work on the latest project. Add `--name <folder>` to use an older one.

## Settings (config.toml)

* `target_seconds`: length of the highlights (default 6 minutes).
* `focus.car_number`: `"auto"` means whoever recorded the replay. You can also set a car number, or leave it empty to treat everyone the same.
* `battle_gap` / `min_battle_seconds`: how close and how long two cars must run together to count as a battle.
* `battle_cameras` etc.: camera group names as they appear in iRacing's camera list.
* `commentary.model`: any Ollama model. `qwen2.5:7b` and `llama3.1:8b` both work well. Bigger models give better lines if you have the VRAM.
* `commentary.style`: the commentator's personality, e.g. "a calm American IndyCar announcer".
* `tts.kokoro_voice`: `bm_george`, `bm_lewis`, `bm_daniel` (British male), `am_michael`, `am_adam` (American male), `bf_emma`, `af_heart` (female), and more.
* `render.game_volume`, `duck_ratio`, `commentary_volume`: the audio mix.

## "A race in 30" (TikTok / Shorts)

Open OBS and the replay, then run `run.bat short` (or double-click `make_short.bat`). It:
1. scans the replay and finds **your** moments: the start, passes you make, passes on you, spins, restarts and the finish (plus the race's best moments if you had a quiet one);
2. opens the review page so you can change any angle, timing or caption;
3. records 2-4 seconds of each moment from the angle set for that kind of moment (`[short]` in config.toml);
4. builds a 30-second vertical video with punchy captions, a live position badge, lap counter and progress bar, ending on your finishing position.

Output: `<race>_race_in_30.mp4`, plus `short\clean\beat_XX.mp4` - the same moments as clean vertical clips with no text, ready to drop into TikTok's editor with its own music and effects.

Options: `--driver 28` (someone else's race), `--reuse` (skip the scan for the last race), `--seconds 60`, `--no-review`. Set `music = "C:/path/song.mp3"` under `[short]` for a music bed. `layout = "crop"` fills the whole screen instead of the zoomed-in middle.

## Better commentary and voice

**Commentary model:** with a 16 GB GPU, run `ollama pull mistral-small3.2`. `config.toml` already uses it. On 8–12 GB cards, use `gemma3:12b` or `llama3.1:8b`. You can use any model from ollama.com/library.

**Chatterbox voice:** double-click `setup_chatterbox.bat` once. It downloads about 5 GB and needs an NVIDIA GPU. Chatterbox sounds more natural and gets louder and more excited for passes, crashes, the start and the finish. Settings under `[tts]`:
* `chatterbox_reference`: a WAV of the voice to copy. Use 10–20 seconds of one person talking clearly with no music, ideally with some energy. Recording yourself doing a commentator voice works well. Leave it empty to copy the British Kokoro voice. Only use a voice you have permission to use.
* `chatterbox_exaggeration`: overall energy (0.6 is normal, 0.8 and up is very excited).
* `chatterbox_cfg`: lower values give faster, punchier delivery.

To re-voice an existing video, run `run.bat voice` and then `run.bat render`.

## Troubleshooting

* **"Couldn't connect to iRacing"**: the replay has to be open and playing in the sim, not just selected in the UI.
* **"Couldn't reach OBS"**: enable the WebSocket server in OBS and check the port and password.
* **The clips are black**: in OBS, switch the Game Capture source to "Capture specific window" and select iRacing. If that doesn't help, try Display Capture.
* **The commentary is generic** ("built-in lines"): Ollama wasn't running. Start it, then run `run.bat commentary`, `run.bat voice` and `run.bat render` again.
* **A clip starts on the wrong car**: iRacing loads cameras slowly on some PCs. Raise `capture.settle_seconds`, then re-record that clip with `--clips N`.
* Full error details go to `output\error.log`.

## Files

```
director/sim.py         iRacing SDK: session info, scanning, seeking, cameras
director/detect.py      overtakes, battles, incidents, pit stops, start/finish
director/plan.py        scoring -> edit list, shot/camera choice
director/capture.py     OBS WebSocket recording
director/commentary.py  Ollama prompts + offline fallback lines
director/tts.py         Kokoro / Piper / Windows voice
director/render.py      ffmpeg cards, captions, ducking, concat
director/fake.py        synthetic race for testing
```

## Licence

iRacing Director is released under the [MIT Licence](LICENSE).

It uses, but does not include, these third-party projects. Each has its own licence:
- [pyirsdk](https://github.com/kutu/pyirsdk) (MIT) to read iRacing telemetry and control replays
- [obsws-python](https://github.com/aatikturk/obsws-python) (GPL-3.0) to control OBS. It's installed separately by `setup.bat`, not bundled.
- [Ollama](https://ollama.com) and the model you choose, for the commentary. Check that model's licence.
- [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M) (Apache-2.0) via [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) (MIT) and [Chatterbox](https://github.com/resemble-ai/chatterbox) (MIT) for the voice. Only clone a voice you have permission to use.
- [FFmpeg](https://ffmpeg.org) (LGPL/GPL) for video
- [Barlow Condensed](https://github.com/jpt/barlow) (SIL Open Font License 1.1), downloaded by `setup.bat`

iRacing is a trademark of iRacing.com Motorsport Simulations. This project isn't affiliated with or endorsed by iRacing.
