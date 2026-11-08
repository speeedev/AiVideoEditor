# AI Video Editor (AVE)

Claude destekli otomatik video editörü. Ham kayıtlarını (ekran kaydı, kamera, harici mikrofon, müzik)
verirsin. Program kayıtları analiz edip sese göre senkronlar ve bir timeline kurar. Claude bu timeline'ı
gerçek bir editör gibi araçlarla düzenler: keser, sıkılaştırır, layout değiştirir, zoom yapar,
hızlandırır, yazı, altyazı ve müzik ekler. Sonuç FFmpeg ile render edilir ya da Premiere, DaVinci Resolve
veya Final Cut'a aktarılır.

```
Kayıtlar ─► Analiz ─────────► Timeline ◄────► Claude (araç kullanımı) ─► Render (FFmpeg)
            transkript         track'ler,       "şu aralığı kes,           ön izleme / final
            sessizlikler       klipler,         kamerayı köşeye al,        veya XML/OTIO/EDL
            ekran aktivitesi   layout, yazı     burada zoom yap…"
            ses senkronu
```

## Nasıl çalışır?

Claude videoyu izleyemez, sesi de duyamaz. O yüzden program her kaydı Claude'un okuyabileceği bir
biçime çevirir:

| Ne                       | Nasıl                                                          | Claude'a nasıl ulaşır |
|--------------------------|----------------------------------------------------------------|-----------------------|
| Konuşma                  | faster-whisper ile kelime zamanlı transkript                   | timeline zamanına çevrilmiş metin |
| Duraklamalar             | enerji tabanlı konuşma/sessizlik tespiti                       | kesim önerileri, kesimleri sessizliğe oturtma |
| Ekranda olanlar          | düşük çözünürlüklü kare farkı (aktivite + sahne kesimi)        | "aktif / boş" aralıklar |
| Görüntü                  | kaynaktan veya kompozit çıktıdan kare                          | zaman damgalı kontak sayfaları (görsel) |
| Senkron                  | ses zarfı çapraz korelasyonu + GCC-PHAT, saat kayması tespiti  | otomatik, milisaltı hassasiyet |

Kayıtlar ortak bir saate oturtulup timeline'a alt alta dizilir. Claude bir aralığı kestiğinde bu kesim
bütün track'lerde aynı anda yapılır (ripple delete), böylece kamera, ekran ve mikrofon hep senkron
kalır. Her kesimden sonra transkript yeni timeline'a yeniden yansıtılır, Claude da her zaman güncel
zamanlarla çalışır.

## Kurulum

Gereksinimler: **Python 3.10+**, **FFmpeg** (PATH'te `ffmpeg` ve `ffprobe`), Anthropic API anahtarı.

```bash
git clone https://github.com/speeedev/AiVideoEditor.git
cd AiVideoEditor
pip install -e ".[transcribe]"
export ANTHROPIC_API_KEY=sk-ant-...
```

- FFmpeg kurulumu: macOS'ta `brew install ffmpeg`, Windows'ta `winget install ffmpeg`, Ubuntu'da
  `sudo apt install ffmpeg`.
- `[transcribe]` ekstrası faster-whisper'ı kurar. İlk kullanımda Whisper modeli internetten indirilir.
  NVIDIA GPU varsa transkripsiyon çok daha hızlıdır.

## Hızlı deneme (sentetik demo)

Elinde kayıt yoksa bile tüm akışı deneyebilirsin:

```bash
ave demo demo-proje          # sentetik ekran + kamera + mikrofon üretir, analiz eder, senkronlar
ave ui demo-proje            # tarayıcıda editörü açar
```

## Kendi kayıtlarınla kullanım

### Web arayüzü (önerilen)

```bash
ave init benim-videom --brief "8-10 dk YouTube eğitimi, samimi anlatım, beklemeler kesilsin"
ave ui benim-videom
```

Tarayıcıda açılan editörde:

1. **Kayıtlar:** dosyaları sürükleyip bırak (ya da yerel yolunu yaz, büyük dosyalar kopyalanmaz) ve her
   birine rol ver: ekran, kamera, mikrofon, müzik.
2. **Analiz:** dili ve Whisper modelini seç, sonra *Analiz*'e bas.
3. **Senkron:** kayıtları sese göre hizalar ve timeline'ı kurar (*Timeline kur*).
4. **Claude paneli:** ne istediğini yaz, örneğin "Hataları ve tekrarları temizle, akıcı bir kurgu yap,
   kelime vurgulu altyazı ekle". Claude'un düşüncelerini, kullandığı araçları ve baktığı kareleri
   canlı izlersin. Timeline da gözünün önünde güncellenir.
5. Timeline'a tıklayınca o anın **kompozit karesi** görünür (layout, zoom, yazı ve altyazı dahil).
6. *Ön izleme render* ile videoyu izle, sonra "girişi biraz daha kısalt" gibi talimatlarla devam et.
   Her değişiklik geri alınabilir (Ctrl+Z).
7. *Final render* tam kalite MP4 üretir. *Dışa aktar* ile Premiere veya Resolve'a geçebilirsin.

### Komut satırı

```bash
ave init benim-videom --width 1920 --height 1080 --fps 30 --brief "YouTube eğitimi"
ave add benim-videom ekran.mp4 --role screen
ave add benim-videom kamera.mp4 --role camera
ave add benim-videom mikrofon.wav --role mic
ave analyze benim-videom --language tr --model small
ave sync benim-videom
ave assemble benim-videom
ave edit benim-videom "Sessizlikleri ve hataları temizle, okunmayan yerlere zoom yap"
ave render benim-videom --preview          # hızlı ön izleme
ave edit benim-videom "Girişi kısalt ve başlık ekle"   # aynı sohbete devam eder
ave render benim-videom -o final.mp4
```

Diğer komutlar:

| Komut | Ne yapar |
|---|---|
| `ave chat PROJE` | terminalde etkileşimli sohbetle düzenleme (`/undo`, `/render`, `/show`) |
| `ave show PROJE` | timeline'ı metin olarak gösterir |
| `ave transcript show PROJE [--words]` | timeline zamanlı transkript |
| `ave transcript import PROJE --asset mic --file x.srt` | hazır transkript (JSON/SRT/VTT) içe aktarır |
| `ave sync PROJE --manual kamera 1.25` | senkronu elle ayarlar |
| `ave add PROJE kayit.mkv --role mic --audio-stream 1` | çok track'li kayıttan (ör. OBS) belirli ses track'ini kullanır |
| `ave still PROJE 12.5 -o kare.jpg` | timeline'ın tek karesini render eder |
| `ave undo PROJE [--steps N]` | son değişiklikleri geri alır |
| `ave export PROJE --format xml\|otio\|edl\|json\|srt` | diğer editörlere aktarır / altyazı dosyası üretir |
| `ave status PROJE` | projenin durumu ve sıradaki adım |
| `ave brief PROJE "metin"` | proje özetini ayarlar |

Model ve düşünme derinliği: `--model claude-opus-5-5` (varsayılan) ve `--effort low|medium|high|xhigh|max`
(varsayılan `high`). Bunlar `AVE_MODEL` ve `AVE_EFFORT` ortam değişkenleriyle de ayarlanabilir.

## Claude'un araçları

| Araç | Ne işe yarar |
|---|---|
| `get_project_overview`, `get_timeline` | kayıtlar, senkron bilgisi, track ve klipler |
| `get_transcript` | timeline veya kaynak zamanlı transkript (cümle ya da kelime düzeyinde) |
| `find_edit_candidates` | uzun duraklama, dolgu kelime (eee/ıı, um/uh) ve tekrar çekim önerileri |
| `get_visual_activity` | ekranın değiştiği ve boş kaldığı aralıklar |
| `view_source`, `view_timeline` | ham kayıttan ya da kompozit çıktıdan kareler (görsel) |
| `ripple_delete` | aralıkları tüm track'lerden keser, boşluğu kapatır, kesimleri sessizliğe oturtur |
| `apply_layout` | `screen_only`, `camera_only`, `pip`, `side_by_side`, `stack` (dikey video) |
| `zoom` | ekran kaydında bir bölgeye yumuşak (animasyonlu) yakınlaşma ve geri açılma |
| `speed_up` | boş beklemeleri hızlandırma |
| `add_music` | konuşma altında otomatik kısılan (ducking) müzik |
| `add_overlay` | logo, slayt, ekran görüntüsü veya b-roll videosu bindirme (konum, boyut, saydamlık, fade) |
| `set_audio_mastering` | final miksin loudness hedefi (varsayılan -14 LUFS, YouTube standardı) |
| `add_text`, `set_captions` | başlık ve yazılar, kelime vurgulu altyazı |
| `place_clip`, `move_clip`, `trim_clip`, `split`, `lift`, `delete_clips`, `set_clip_properties` | klip düzeyinde ince ayar |
| `set_track` | sessize alma, gizleme, kilitleme, ducking, **gürültü azaltma** ve **ses iyileştirme** |
| `add_marker` | insan editöre not |
| `set_canvas` | çıktı boyutu: yatay 1080p/4K, dikey 1080×1920 (Shorts/Reels), kare |
| `save_text` | YouTube başlığı, açıklama, bölüm (chapter) listesi gibi metinleri `exports/` altına kaydeder |
| `undo`, `render_preview`, `assemble_timeline` | geri alma, ön izleme, baştan kurma |

Başarısız bir araç çağrısı hiçbir şeyi değiştirmez. Her değişiklik `history/` klasörüne kaydedilir, bu
yüzden farklı çalıştırmalar arasında da geri alınabilir.

## Dışa aktarma

- **FCP7 XML (`xml`):** Premiere Pro, DaVinci Resolve ve Final Cut içe aktarır.
- **OpenTimelineIO (`otio`):** Resolve 18+ ve pek çok iş akışı.
- **EDL:** tek video ve tek ses track'i (CMX3600).
- **SRT:** kurgulanmış videonun altyazı dosyası (YouTube'a ayrı altyazı olarak yüklemek için).

Kesimler, track yerleşimi, hız ve ses seviyeleri aktarılır. Layout kutuları, zoom, yazılar ve altyazı bu
programın kendi render motoruna özgüdür, aktarılmaz. Bunlar için videoyu render et ya da NLE'de yeniden
oluştur.

## Proje klasörü

```
benim-videom/
  project.json        kayıtlar, roller, senkron ofsetleri, proje özeti
  timeline.json       güncel kurgu
  history/            her değişiklikten önceki timeline (geri alma)
  analysis/<kayıt>/   transcript.json, speech.json, visual.json
  agent/              Claude sohbeti (conversation.json) ve araç günlüğü (log.jsonl)
  renders/            ön izleme ve final videolar
  exports/            XML / OTIO / EDL
  media/              arayüzden yüklenen dosyalar
```

## Mimari

```
src/aivideoeditor/
  media/        ffmpeg/ffprobe sarmalayıcıları
  analysis/     ses çözme, konuşma/sessizlik, görsel aktivite, kareler, transkript, senkron
  timeline/     veri modeli, düzenleme işlemleri, layout/zoom, transkript eşleme, özetler
  render/       parçalı FFmpeg render motoru, ASS altyazı üretimi
  agent/        Claude araç tanımları, sistem promptu, streaming araç döngüsü
  pipeline.py   analiz ve senkron adımları
  session.py    undo, render ve kontak sayfası yardımcıları
  export.py     FCP7 XML, OTIO ve EDL dışa aktarma
  web.py        yerel web sunucusu (stdlib) ve webui/ tek sayfa uygulaması
  cli.py        `ave` komut satırı
```

- **Render:** timeline zaman parçalarına bölünür. Her parça tek bir ffmpeg çalıştırmasıdır: klipler
  doğru noktadan açılır, `overlay` ile birleştirilir ve ses PCM olarak örnek hassasiyetinde karıştırılır.
  Parçalar kayıpsız birleştirilir. Çok kesimli, 1 saatlik bir kayıt da aynı güvenilirlikte render olur.
- **Ajan:** `claude-opus-5-5` ile streaming araç döngüsü kullanılır. Adaptif düşünme, prompt caching ve
  sunucu taraflı fallback açıktır, araç girdileri pydantic ile doğrulanır. Sohbet yalnızca sona ekleme
  yapılarak diske yazılır. Görseller araç sonuçlarının içinde gönderilir.

## Sınırlamalar ve yol haritası

- Sesi olmayan kayıtlar (örneğin sessiz ekran kaydı) sese göre senkronlanamaz. Bunlar referansla aynı
  anda başlamış kabul edilir; `ave sync --manual` ile düzeltilebilir.
- Whisper dolgu kelimeleri bazen atlar. Duraklama analizi bunu büyük ölçüde telafi eder.
- Planlananlar: klipler arası geçiş efektleri (crossfade), müzik ritmine kesim, timeline'da
  sürükle-bırak düzenleme.

## Geliştirme

```bash
pip install -e ".[dev]"
pytest
```

Testler sentetik medya üretir: senkron doğruluğu ve saat kayması, analiz, timeline işlemleri, örnek
hassasiyetinde parçalı render, sahte istemciyle ajan döngüsü, CLI ve web sunucusu.
