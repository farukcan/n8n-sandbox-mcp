# n8n AI Agent için gVisor Sandbox (Dokploy)

n8n'deki AI Agent'a güvenli bir terminal verir. Her sohbet kendi izole konteynerini alır;
konteynerler **gVisor (runsc)** ile çalışır, n8n'in ağına ve sırlarına erişemez, boşta kalınca silinir.

```
n8n AI Agent ── MCP Client Tool ──▶ sandbox-mcp (bu repo) ──▶ sbx-<oturum> konteynerleri (gVisor)
                (HTTP Streamable,          token korumalı,           internet: açık
                 Bearer token)             portu dışarı açık değil    host / iç ağ / diğer konteynerler: kapalı
```

Ajanın araçları: `run_command`, `run_python`, `write_file`, `read_file`, `reset_sandbox`.

## Dosyalar

| Dosya | Ne işe yarar |
|---|---|
| `host/setup-host.sh` | VPS'e **bir kez** SSH ile çalıştırılır: gVisor kurar, Docker'a tanıtır, güvenlik duvarını kurar |
| `docker-compose.yml` | Dokploy'da deploy edilen servis |
| `server.py` | MCP sunucusu (oturum başına sandbox açar/kapatır) |
| `sandbox-image/Dockerfile` | Ajanın çalıştığı ortam (Python 3.12 + hazır paketler). Değiştirirsen sunucu imajı kendisi yeniden derler |

---

## Adım 1 — VPS'e gVisor kur (SSH, tek sefer)

`host/setup-host.sh` dosyasını sunucuya kopyala (scp ile ya da `nano setup-host.sh` açıp içeriği yapıştır) ve çalıştır:

```bash
sudo bash setup-host.sh
```

Script sonunda şunları görmelisin:

- `✓ Docker sees the runsc runtime`
- `✓ gVisor works`
- `✓ internet access works`
- `✓ the host (SSH ...) is NOT reachable from sandboxes`

Notlar:
- Çalışan konteynerlerin (n8n, Dokploy) yeniden başlatılmaz; Docker sadece `reload` edilir.
- VPS'lerde KVM olmadığı için gVisor `systrap` modunda çalışır, bu normaldir.
- Güvenlik duvarı kuralları Docker'ın varsayılan `docker0` ağına uygulanır ve reboot sonrası otomatik gelir
  (`n8n-sandbox-firewall.service`). O ağda başka konteynerin yoksa hiçbir şeyi etkilemez.

## Adım 2 — Repoyu GitHub'a koy

Bu klasörü **private** bir GitHub reposuna push'la (Dokploy build için kaynağa ihtiyaç duyuyor).

## Adım 3 — Dokploy'da deploy et

1. Token üret (bilgisayarında ya da sunucuda): `openssl rand -hex 32`
2. Dokploy → projen → **Create Service → Compose**
3. **Compose Type: Docker Compose** (Stack değil — Stack modu `build` desteklemez)
4. Provider: GitHub → repo ve branch'i seç, **Compose Path:** `./docker-compose.yml`
5. **Environment** sekmesi:
   ```
   MCP_TOKEN=<1. adımdaki token>
   ```
   (Diğer ayarlar opsiyonel, tablo aşağıda.)
6. **Domain ekleme.** Bu servis sadece iç ağdan erişilmeli.
7. **Deploy**. İlk deploy'da sandbox imajı derlenir (birkaç dakika). Logs'ta şunları bekle:
   ```
   Sandbox image n8n-sandbox:latest ready
   MCP endpoint listening on http://0.0.0.0:8000/mcp
   ```

## Adım 4 — n8n servise ulaşabiliyor mu?

SSH'ta:

```bash
# İkisi de listede olmalı
docker network inspect dokploy-network --format '{{range .Containers}}{{.Name}}{{"\n"}}{{end}}' | grep -iE "n8n|sandbox"

# n8n konteynerinin içinden sağlık kontrolü -> "ok" yazmalı
N8N=$(docker ps --format '{{.Names}}' | grep -i n8n | grep -viE "worker|runner|postgres|redis" | head -1)
docker exec "$N8N" node -e "fetch('http://sandbox-mcp:8000/health').then(r=>r.text()).then(console.log)"
```

n8n `dokploy-network`'te değilse (ör. n8n'e domain eklemediysen ya da Isolated Deployments açıksa),
n8n'in compose dosyasında n8n servisine şunu ekleyip yeniden deploy et:

```yaml
services:
  n8n:
    networks:
      - default
      - dokploy-network
networks:
  dokploy-network:
    external: true
```

## Adım 5 — n8n'de bağla

1. **Credentials → New → Bearer Auth**: Token = `MCP_TOKEN` değerin.
2. AI Agent node'unda **Tool → MCP Client Tool** ekle:
   - **Endpoint:** `http://sandbox-mcp:8000/mcp`
   - **Server Transport:** HTTP Streamable
   - **Authentication:** Bearer Auth → az önceki credential
   - **Tools to Include:** All
   - Options'ta **Timeout** varsa `600000` (10 dk) yap. Yapmazsan uzun komutlar n8n tarafında zaman aşımına düşer;
     bu yüzden varsayılan komut süresi 55 sn tutuldu.
3. AI Agent'ın **System Message**'ına şunu ekle (Chat Trigger kullanıyorsan):

   ```
   Bir Linux sandbox'ına erişimin var (run_command, run_python, write_file, read_file, reset_sandbox).
   Tüm sandbox araçlarında session_id olarak HER ZAMAN şunu kullan: {{ $('When chat message received').item.json.sessionId }}
   Karmaşık işleri adım adım yap: önce kodu dosyaya yaz, sonra çalıştır, hatayı okuyup düzelt.
   Sonuç dosyalarını read_file ile oku ve kullanıcıya özetle.
   ```

   Telegram gibi başka bir trigger kullanıyorsan session_id olarak sohbet kimliğini ver, ör.
   `{{ $('Telegram Trigger').item.json.message.chat.id }}`.

## Adım 6 — Test

Chat'e sırayla yaz:

1. `Sandbox'ta dmesg komutunun ilk satırını ve python sürümünü göster.` → çıktıda **Starting gVisor** görmelisin.
2. `pandas ile 1'den 10'a kadar sayıların karelerini içeren bir tablo oluştur, result.csv olarak kaydet ve içeriğini göster.`
3. `172.17.0.1 adresinin 22 portuna bağlanmayı dene.` → bağlanamamalı (güvenlik duvarı çalışıyor demek).

Dokploy'da sandbox-mcp loglarında her komut `session=... run_command: ...` olarak görünür.

---

## Ayarlar (Dokploy Environment)

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `MCP_TOKEN` | — (zorunlu) | En az 24 karakter. `openssl rand -hex 32` |
| `SANDBOX_NETWORK` | `bridge` | `none` = sandbox'ta hiç internet yok (pip install çalışmaz, en güvenlisi) |
| `SANDBOX_DNS` | `1.1.1.1,9.9.9.9` | Sandbox'ların kullandığı DNS sunucuları |
| `MAX_SANDBOXES` | `5` | Aynı anda en fazla kaç oturum |
| `SANDBOX_MEMORY` | `1g` | Sandbox başına RAM |
| `SANDBOX_CPUS` | `1.0` | Sandbox başına CPU |
| `IDLE_TIMEOUT_MINUTES` | `30` | Boşta kalan sandbox bu süre sonra silinir |
| `MAX_LIFETIME_MINUTES` | `360` | Bir sandbox en fazla bu kadar yaşar |
| `DEFAULT_TIMEOUT_SECONDS` | `55` | Komut başına varsayılan süre (n8n timeout'unu artırdıysan yükseltebilirsin) |
| `MAX_TIMEOUT_SECONDS` | `900` | Ajanın isteyebileceği en uzun süre |

Ek paket/araç lazımsa `sandbox-image/Dockerfile`'a ekle, push'la, redeploy et; imaj otomatik yeniden derlenir.

## Güvenlik notları

- `sandbox-mcp` Docker soketine eriştiği için **host'ta root yetkisine eşdeğer**. Ona asla domain ya da port verme,
  token'ı kimseyle paylaşma.
- Sandbox'lara hiçbir API anahtarı / env değişkeni geçirilmez. Ajanın sandbox'a koyduğu veri internete gidebilir
  (internet açık olduğu için); hassas veriyle çalışıyorsan `SANDBOX_NETWORK=none` kullan.
- Sandbox'lar: gVisor + root olmayan kullanıcı + tüm Linux capability'leri kapalı + `no-new-privileges`
  + RAM/CPU/process limiti + host, iç ağlar, bulut metadata adresi ve SMTP 25 engelli.
- gVisor'ı güncel tut: `sudo apt-get update && sudo apt-get install --only-upgrade runsc`

## Sorun giderme

| Belirti | Çözüm |
|---|---|
| Logs: `Docker runtime 'runsc' is not installed` | Adım 1'i çalıştır, sonra redeploy |
| Logs: `MCP_TOKEN must be set...` | Environment'a en az 24 karakterlik token ekle |
| Deploy: `network dokploy-network not found` | `docker network ls` ile kontrol et; Compose Type'ın "Docker Compose" olduğundan emin ol |
| n8n: bağlanamıyor / `ENOTFOUND sandbox-mcp` | Adım 4 — ikisi aynı ağda değil |
| n8n: 401 | Bearer token yanlış |
| n8n: zaman aşımı | MCP Client Tool Timeout'u artır ya da `DEFAULT_TIMEOUT_SECONDS`'u düşür |
| Sandbox'ta `pip install` / DNS çalışmıyor | `docker run --rm --runtime=runsc --dns 1.1.1.1 busybox nslookup pypi.org` ile test et; sağlayıcın dış DNS'i engelliyorsa `SANDBOX_DNS` ile başka DNS ver |
| `Sandbox limit reached` | `MAX_SANDBOXES`'ı artır veya `IDLE_TIMEOUT_MINUTES`'ı düşür |
| Dokploy temizliği sandbox imajını sildi | Sorun değil: ilk komutta imaj otomatik yeniden derlenir; o ilk çağrı zaman aşımına düşebilir, birkaç dakika sonra tekrar dene |
