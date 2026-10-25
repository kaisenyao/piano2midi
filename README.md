<h1 align="center">piano2midi</h1>

<p align="center">
  Solo piano recording in, performance MIDI out.<br>
  Notes, timing, velocity and sustain pedal, ready for Pianoteq or a self-playing piano.
</p>

<p align="center">
  <img src="demo/pianoteq-playback.gif" alt="A transcribed Glenn Gould recording playing in Pianoteq" width="720">
</p>

<p align="center">
  <sub>Glenn Gould's rehearsal of Bach's Partita No. 2, transcribed by piano2midi and played back in Pianoteq 9 (20-second excerpt).</sub>
</p>

## Demo

**Glenn Gould: J.S. Bach, Partita No. 2 in C minor, BWV 826 (rehearsal recording)**

<table>
  <tr>
    <th width="50%">Original recording</th>
    <th width="50%">Transcription, played in Pianoteq 9</th>
  </tr>
  <tr>
    <td valign="top">
      <video src="https://github.com/user-attachments/assets/4d7ad6ef-2430-4907-9b99-27893429c51e" width="100%" controls></video>
    </td>
    <td valign="top">
      <video src="https://github.com/user-attachments/assets/66c23753-2c38-4d77-9b6e-b4bc9aa24dfe" width="100%" controls></video>
    </td>
  </tr>
  <tr>
    <td><sub>Source: <a href="https://www.youtube.com/watch?v=WqwZC-yLYI4">Glenn Gould Official Channel on YouTube</a>.</sub></td>
    <td><sub>NY Steinway D Classical preset, on iPad. Full length, with sound.</sub></td>
  </tr>
</table>

| Audio | Notes | Velocity | Sustain pedal | Settings | Time |
|---|---|---|---|---|---|
| 1 min 47 s | 1,229 | 36–93 | 1 short press | default, no manual edits | about 1 min on 2 CPU cores |

Gould plays this music almost without pedal, and the transcription reflects that. Download the MIDI: [`demo/gould-partita-2-rehearsal.mid`](demo/gould-partita-2-rehearsal.mid)

## 日常使用

1. 在 `tasks.csv` 填写视频链接，每行一个，只保留 `url` 表头也可以：

   ```csv
   url
   https://www.youtube.com/watch?v=你的视频ID
   ```

2. 在项目目录运行：

   ```bash
   ./run.sh
   ```

3. 到 `output/` 找 MIDI。文件名自动取视频标题；如需自定义曲名，可添加 `name` 列。

程序会补齐处理状态和文件路径。成功项自动跳过，失败或中断后运行同一命令即可继续。
下载的原格式音频缓存在 `output/audio/`，不经过 MP3 二次压缩；原始 MIDI 和统计在
`output/extras/`。不要在运行期间编辑 CSV，同一清单一次只运行一个进程。

## 首次安装

需要 macOS 或 Linux、Python 3.10+（或 uv）。在项目目录运行：

```bash
./setup.sh
```

安装完成后会创建空白 `tasks.csv`，已有清单不会被覆盖。ffmpeg 随依赖提供，
YouTube 下载还需要本机安装 Deno 或 Node.js。`tasks.csv` 和 `output/` 不提交到 Git。

## 其他用法

```bash
# 指定清单、设备和 CPU 线程数
./run.sh --csv tasks.csv --device cpu --threads 2

# 重新转录，复用已下载的有效音频
./run.sh --csv tasks.csv --overwrite

# 直接转换本地音频文件或目录，不需要 input 文件夹
./run.sh /path/to/song.mp3
./run.sh /path/to/recordings -o output
```

CSV 列为 `url,name,status,audio_path,midi_path,error`，仅 `url` 必填。
状态为 `pending`、`processing`、`done` 或 `failed`，空白视为待处理。
保留生成的路径列以便恢复；更换链接时新增一行。路径相对 CSV 所在目录保存，
已分配的输出路径在重试时保持固定。新任务使用 `-o` 指定的目录。
单项失败返回 1，清单或用法错误返回 2，中断返回 130。

清理默认修正同音重叠、移除弱音踏板并确保末尾松开延音踏板。
可用 `--legato`、`--keep-soft-pedal`、`--pedal-ramps` 调整，详见 `./run.sh --help`。

## 限制与维护

适合独奏钢琴，不进行人声／伴奏分离；识别可能有漏音或错音，延音踏板只支持开关。
仅下载你有权获取的单个视频；不展开播放列表，不支持直播或绕过访问限制。
网站支持取决于 yt-dlp，下载失败会记录在清单中。更新下载器可运行：

```bash
uv pip install --python .venv/bin/python --upgrade 'yt-dlp[default]'
```

`src/` 是核心代码，`tests/` 用于检查功能是否正常。运行测试：

```bash
uv pip install --python .venv/bin/python pytest
.venv/bin/python -m pytest
```

项目采用 MIT 许可证。转录模型来自 [Transkun](https://github.com/Yujia-Yan/Transkun)；
其预训练权重基于 MAESTRO 数据集（CC BY-NC-SA 4.0），商业使用前请核实相关条款。
