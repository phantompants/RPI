/*
  CYD (ESP32-2432S028 "Cheap Yellow Display") remote screen for the RPi 4WD GPS Tracker

  - Joins the vehicle Wi-Fi (GL.iNet) and polls the Pi's API: http://<pi>:8000/api/gps and /api/sats
  - Tap the screen to cycle pages: Dashboard -> Inclinometer -> Satellites
  - First boot (or no saved Wi-Fi): creates hotspot "GPS-Display-Setup". Join it from a phone,
    pick the vehicle Wi-Fi, enter its password and the Pi's name/IP (default gps4.local).
  - Hold a finger on the screen while powering on to clear Wi-Fi settings and re-run setup.
  - RGB LED: green = live data, red = no link to the Pi, blue = Wi-Fi setup.
  - Serial (115200) commands for bench setup: i = toggle colour inversion, d = toggle display
    driver (ILI9341/ST7789), r = next rotation, w = forget Wi-Fi, s = status.

  Libraries: LovyanGFX, WiFiManager (tzapu), ArduinoJson 7.
*/

#include <WiFi.h>
#include <HTTPClient.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <SPI.h>
#include <WiFiManager.h>
#include <ArduinoJson.h>
#define LGFX_USE_V1
#include <LovyanGFX.hpp>

// ---------- CYD pins ----------
constexpr int TFT_SCLK = 14, TFT_MOSI = 13, TFT_MISO = 12, TFT_CS = 15, TFT_DC = 2, TFT_BL = 21;
constexpr int TOUCH_SCLK = 25, TOUCH_MOSI = 32, TOUCH_MISO = 39, TOUCH_CS = 33, TOUCH_IRQ = 36;
constexpr int LED_R = 4, LED_G = 16, LED_B = 17;   // active low

constexpr uint32_t GPS_POLL_MS = 500;
constexpr uint32_t SATS_POLL_MS = 2000;
constexpr uint32_t STALE_MS = 3000;                // no good reply for this long = "NO LINK"

// ---------- Display (driver chosen at runtime: USB-C CYDs may use ST7789 instead of ILI9341) ----------
class LGFX : public lgfx::LGFX_Device {
  lgfx::Panel_ILI9341 _ili;
  lgfx::Panel_ST7789 _st;
  lgfx::Bus_SPI _bus;
  lgfx::Light_PWM _light;
  lgfx::Touch_XPT2046 _touch;
public:
  void setup(bool st7789, bool invert) {
    {
      auto cfg = _bus.config();
      cfg.spi_host = HSPI_HOST;
      cfg.spi_mode = 0;
      cfg.freq_write = 40000000;
      cfg.freq_read = 16000000;
      cfg.spi_3wire = false;
      cfg.use_lock = true;
      cfg.dma_channel = SPI_DMA_CH_AUTO;
      cfg.pin_sclk = TFT_SCLK;
      cfg.pin_mosi = TFT_MOSI;
      cfg.pin_miso = TFT_MISO;
      cfg.pin_dc = TFT_DC;
      _bus.config(cfg);
    }
    lgfx::Panel_Device* panel = st7789 ? static_cast<lgfx::Panel_Device*>(&_st) : &_ili;
    panel->setBus(&_bus);
    {
      auto cfg = panel->config();
      cfg.pin_cs = TFT_CS;
      cfg.pin_rst = -1;
      cfg.pin_busy = -1;
      cfg.memory_width = 240;
      cfg.memory_height = 320;
      cfg.panel_width = 240;
      cfg.panel_height = 320;
      cfg.offset_x = 0;
      cfg.offset_y = 0;
      cfg.offset_rotation = 0;
      cfg.dummy_read_pixel = 8;
      cfg.dummy_read_bits = 1;
      cfg.readable = true;
      cfg.invert = invert;
      cfg.rgb_order = false;
      cfg.dlen_16bit = false;
      cfg.bus_shared = false;
      panel->config(cfg);
    }
    {
      auto cfg = _light.config();
      cfg.pin_bl = TFT_BL;
      cfg.invert = false;
      cfg.freq = 12000;
      cfg.pwm_channel = 7;
      _light.config(cfg);
      panel->setLight(&_light);
    }
    {
      auto cfg = _touch.config();
      cfg.x_min = 300;
      cfg.x_max = 3900;
      cfg.y_min = 200;
      cfg.y_max = 3700;
      cfg.pin_int = TOUCH_IRQ;
      cfg.bus_shared = false;
      cfg.offset_rotation = 0;
      cfg.spi_host = VSPI_HOST;
      cfg.freq = 1000000;
      cfg.pin_sclk = TOUCH_SCLK;
      cfg.pin_mosi = TOUCH_MOSI;
      cfg.pin_miso = TOUCH_MISO;
      cfg.pin_cs = TOUCH_CS;
      _touch.config(cfg);
      panel->setTouch(&_touch);
    }
    setPanel(panel);
  }
};

LGFX lcd;
LGFX_Sprite frame(&lcd);   // full-screen 8-bit back buffer (76.8 KB) so redraws don't flicker
Preferences prefs;

// ---------- Settings ----------
struct Settings {
  bool st7789 = false;
  bool invert = false;
  uint8_t rotation = 1;
  String piHost = "gps4.local";
} settings;

// ---------- Live data ----------
struct Telemetry {
  float speed = 0, heading = 0, alt = 0, hdop = 99.9;
  float pitch = 0, roll = 0, g = 1, roughness = 0;
  int sats = 0, satsVisible = 0, fix = 0, rows = 0;
  bool imu = false;
  String status = "--", mode = "", logFile = "";
} tel;

struct Sat { int prn; float el, az, snr; bool used; char gnss[8]; };
Sat sats[40];
int satCount = 0;

IPAddress piIP;
bool piResolved = false;
uint32_t lastGoodMs = 0, lastGpsPoll = 0, lastSatsPoll = 0;
int page = 0;
constexpr int PAGE_COUNT = 3;

// ---------- Helpers ----------
void setLed(bool r, bool g, bool b) {
  digitalWrite(LED_R, r ? LOW : HIGH);
  digitalWrite(LED_G, g ? LOW : HIGH);
  digitalWrite(LED_B, b ? LOW : HIGH);
}

void loadSettings() {
  prefs.begin("cyd", true);
  settings.st7789 = prefs.getBool("st7789", settings.st7789);
  settings.invert = prefs.getBool("invert", settings.invert);
  settings.rotation = prefs.getUChar("rot", settings.rotation);
  settings.piHost = prefs.getString("pihost", settings.piHost);
  bool detected = prefs.isKey("st7789");
  prefs.end();
  if (!detected) settings.st7789 = detectST7789();
}

void saveSettings() {
  prefs.begin("cyd", false);
  prefs.putBool("st7789", settings.st7789);
  prefs.putBool("invert", settings.invert);
  prefs.putUChar("rot", settings.rotation);
  prefs.putString("pihost", settings.piHost);
  prefs.end();
}

// Read the panel ID (RDDID 0x04). ST7789 answers 0x85 0x85 0x52 (after a 1-bit dummy clock,
// so check every bit alignment). Anything else is treated as ILI9341.
bool detectST7789() {
  SPIClass spi(HSPI);
  spi.begin(TFT_SCLK, TFT_MISO, TFT_MOSI, -1);
  pinMode(TFT_CS, OUTPUT);
  pinMode(TFT_DC, OUTPUT);
  digitalWrite(TFT_CS, HIGH);
  delay(5);
  spi.beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE0));
  digitalWrite(TFT_CS, LOW);
  digitalWrite(TFT_DC, LOW);
  spi.transfer(0x04);
  digitalWrite(TFT_DC, HIGH);
  uint64_t raw = 0;
  for (int i = 0; i < 5; i++) raw = (raw << 8) | spi.transfer(0x00);
  digitalWrite(TFT_CS, HIGH);
  spi.endTransaction();
  spi.end();
  bool st = false;
  for (int shift = 0; shift <= 16; shift++) {
    if (((raw >> shift) & 0xFFFFFF) == 0x858552) st = true;
  }
  Serial.printf("[LCD] RDDID raw=0x%010llX -> %s\n", raw, st ? "ST7789" : "ILI9341");
  return st;
}

bool resolvePi() {
  String host = settings.piHost;
  if (piIP.fromString(host)) return piResolved = true;
  if (host.endsWith(".local")) host = host.substring(0, host.length() - 6);
  IPAddress ip = MDNS.queryHost(host.c_str(), 2000);
  if (ip != IPAddress(0, 0, 0, 0)) {
    piIP = ip;
    Serial.printf("[NET] %s -> %s\n", settings.piHost.c_str(), ip.toString().c_str());
    return piResolved = true;
  }
  Serial.printf("[NET] Could not resolve %s\n", settings.piHost.c_str());
  return piResolved = false;
}

bool fetchJson(const char* path, JsonDocument& doc, uint16_t timeoutMs) {
  if (!piResolved && !resolvePi()) return false;
  HTTPClient http;
  http.setConnectTimeout(timeoutMs);
  http.setTimeout(timeoutMs);
  String url = "http://" + piIP.toString() + ":8000" + path;
  if (!http.begin(url)) return false;
  int code = http.GET();
  bool ok = false;
  if (code == 200) {
    ok = !deserializeJson(doc, http.getString());
  } else if (code < 0) {
    piResolved = false;  // Pi may have a new address: resolve again next time
  }
  http.end();
  return ok;
}

void pollGps() {
  JsonDocument doc;
  if (!fetchJson("/api/gps", doc, 800)) return;
  tel.speed = doc["speed"] | 0.0f;
  tel.heading = doc["heading"] | 0.0f;
  tel.alt = doc["alt"] | 0.0f;
  tel.hdop = doc["hdop"] | 99.9f;
  tel.pitch = doc["pitch"] | 0.0f;
  tel.roll = doc["roll"] | 0.0f;
  tel.g = doc["g_total"] | 1.0f;
  tel.roughness = doc["roughness"] | 0.0f;
  tel.sats = doc["satellites"] | 0;
  tel.satsVisible = doc["sats_visible"] | 0;
  tel.fix = doc["fix_quality"] | 0;
  tel.rows = doc["points_logged_today"] | 0;
  tel.imu = doc["imu_connected"] | false;
  tel.status = (const char*)(doc["status"] | "--");
  tel.mode = (const char*)(doc["mode"] | "");
  tel.logFile = (const char*)(doc["current_log_file"] | "");
  lastGoodMs = millis();
}

void pollSats() {
  JsonDocument doc;
  if (!fetchJson("/api/sats", doc, 1500)) return;
  satCount = 0;
  for (JsonObject s : doc["satellites"].as<JsonArray>()) {
    if (satCount >= 40) break;
    Sat& t = sats[satCount++];
    t.prn = s["prn"] | 0;
    t.el = s["el"].isNull() ? -1 : s["el"].as<float>();
    t.az = s["az"].isNull() ? -1 : s["az"].as<float>();
    t.snr = s["snr"] | 0.0f;
    t.used = s["used"] | false;
    strlcpy(t.gnss, s["gnss"] | "?", sizeof(t.gnss));
  }
}

// ---------- Drawing ----------
constexpr uint32_t C_BG = 0x0A0E17, C_CARD = 0x141B2D, C_CYAN = 0x00F0FF, C_GREEN = 0x00FF88;
constexpr uint32_t C_RED = 0xFF3366, C_GOLD = 0xFFAA00, C_DIM = 0x8A99AD, C_WHITE = 0xFFFFFF;
constexpr uint32_t C_GRID = 0x39414F, C_TRACK = 0x1E2638;
constexpr uint32_t C_PURE_R = 0xFF0000, C_PURE_G = 0x00FF00, C_PURE_B = 0x0000FF;

uint32_t snrColour(float snr) {
  if (snr >= 30) return C_GREEN;
  if (snr >= 20) return C_GOLD;
  if (snr > 0) return C_RED;
  return C_DIM;
}

const char* compass(float deg) {
  static const char* pts[] = {"N", "NE", "E", "SE", "S", "SW", "W", "NW"};
  return pts[(int)((deg + 22.5f) / 45.0f) % 8];
}

bool linkOk() { return lastGoodMs && millis() - lastGoodMs < STALE_MS; }

void drawHeader(const char* title) {
  frame.fillRect(0, 0, 320, 26, C_CARD);
  frame.setFont(&fonts::FreeSansBold9pt7b);
  frame.setTextDatum(middle_left);
  frame.setTextColor(C_CYAN);
  frame.drawString(title, 6, 13);

  bool ok = linkOk();
  const char* fixText = !ok ? "NO LINK" : tel.fix >= 3 ? "3D FIX" : tel.fix == 2 ? "2D FIX" : "NO FIX";
  uint32_t fixCol = (!ok || tel.fix < 2) ? C_RED : C_GREEN;
  frame.setFont(&fonts::Font2);
  int w = frame.textWidth(fixText) + 12;
  frame.fillRoundRect(314 - w, 4, w, 18, 9, fixCol);
  frame.setTextColor(C_BG);
  frame.setTextDatum(middle_center);
  frame.drawString(fixText, 314 - w / 2, 13);

  frame.setTextColor(C_WHITE);
  frame.setTextDatum(middle_right);
  char buf[24];
  snprintf(buf, sizeof(buf), "SAT %d/%d", tel.sats, tel.satsVisible);
  frame.drawString(buf, 314 - w - 8, 13);
}

void drawPageDots() {
  for (int i = 0; i < PAGE_COUNT; i++) {
    frame.fillCircle(160 + (i - 1) * 12, 234, 3, i == page ? C_CYAN : C_DIM);
  }
}

void drawStatBox(int x, int y, int w, int h, const char* label, const char* value, uint32_t col) {
  frame.fillRoundRect(x, y, w, h, 6, C_CARD);
  frame.setFont(&fonts::Font0);
  frame.setTextColor(C_DIM);
  frame.setTextDatum(top_center);
  frame.drawString(label, x + w / 2, y + 4);
  frame.setFont(&fonts::FreeSansBold12pt7b);
  frame.setTextColor(col);
  frame.setTextDatum(middle_center);
  frame.drawString(value, x + w / 2, y + h / 2 + 6);
}

// Numbers with a degree ring (the GFX fonts have no degree glyph)
void drawDegrees(int value, int cx, int cy, const lgfx::IFont* font, uint32_t col, int ring) {
  char buf[12];
  snprintf(buf, sizeof(buf), "%d", value);
  frame.setFont(font);
  frame.setTextColor(col);
  frame.setTextDatum(middle_center);
  frame.drawString(buf, cx, cy);
  int half = frame.textWidth(buf) / 2;
  frame.drawCircle(cx + half + ring + 2, cy - frame.fontHeight() / 3, ring, col);
}

void drawDashboard() {
  drawHeader("4WD TRACKER");
  char buf[32];

  // Speed
  frame.fillRoundRect(4, 32, 176, 110, 8, C_CARD);
  frame.setFont(&fonts::Font7);
  frame.setTextColor(linkOk() ? C_CYAN : C_DIM);
  frame.setTextDatum(middle_center);
  snprintf(buf, sizeof(buf), "%d", (int)roundf(tel.speed));
  frame.drawString(buf, 92, 80);
  frame.setFont(&fonts::FreeSansBold9pt7b);
  frame.setTextColor(C_GOLD);
  frame.drawString("km/h", 92, 128);

  // Heading / altitude / HDOP
  snprintf(buf, sizeof(buf), "%s %d", compass(tel.heading), (int)tel.heading);
  drawStatBox(186, 32, 130, 34, "HEADING", buf, C_WHITE);
  snprintf(buf, sizeof(buf), "%d m", (int)tel.alt);
  drawStatBox(186, 70, 130, 34, "ALTITUDE", buf, C_WHITE);
  snprintf(buf, sizeof(buf), "%.1f", tel.hdop);
  drawStatBox(186, 108, 130, 34, "HDOP", buf, tel.hdop <= 2 ? C_GREEN : tel.hdop <= 5 ? C_GOLD : C_RED);

  // IMU row
  drawStatBox(4, 148, 101, 44, "PITCH", "", C_WHITE);
  drawDegrees((int)roundf(tel.pitch), 54, 176, &fonts::FreeSansBold12pt7b, fabsf(tel.pitch) > 25 ? C_RED : C_WHITE, 3);
  drawStatBox(109, 148, 101, 44, "ROLL", "", C_WHITE);
  drawDegrees((int)roundf(tel.roll), 159, 176, &fonts::FreeSansBold12pt7b, fabsf(tel.roll) > 20 ? C_RED : C_WHITE, 3);
  snprintf(buf, sizeof(buf), "%.2fg", tel.g);
  drawStatBox(214, 148, 102, 44, "G-FORCE", buf, (tel.g > 1.3f || tel.g < 0.7f) ? C_RED : C_GREEN);

  // Footer: logging
  frame.setFont(&fonts::Font0);
  frame.setTextDatum(middle_left);
  frame.setTextColor(C_DIM);
  snprintf(buf, sizeof(buf), "LOG %d rows", tel.rows);
  frame.drawString(buf, 6, 204);
  frame.setTextDatum(middle_right);
  frame.drawString(tel.logFile.c_str(), 314, 204);
  frame.setTextDatum(middle_left);
  frame.drawString(tel.imu ? "IMU OK" : "IMU --", 6, 218);
  frame.setTextDatum(middle_right);
  snprintf(buf, sizeof(buf), "WiFi %d dBm", WiFi.RSSI());
  frame.drawString(buf, 314, 218);
}

// Inclinometer: side view (pitch) and rear view (roll) of a simple vehicle outline
void drawVehicle(int cx, int cy, float angleDeg, bool side, const char* label, uint32_t warnCol) {
  frame.fillRoundRect(cx - 76, cy - 80, 152, 176, 8, C_CARD);
  float a = -angleDeg * DEG_TO_RAD;
  float ca = cosf(a), sa = sinf(a);
  auto rot = [&](float x, float y, int& ox, int& oy) {
    ox = cx + (int)(x * ca - y * sa);
    oy = cy + (int)(x * sa + y * ca);
  };
  // Horizon line
  frame.drawFastHLine(cx - 70, cy + 30, 140, C_DIM);
  // Body polygon
  const float sideBody[][2] = {{-55, 20}, {55, 20}, {55, -2}, {30, -8}, {18, -28}, {-45, -28}, {-55, -8}};
  const float rearBody[][2] = {{-40, 20}, {40, 20}, {40, -8}, {30, -32}, {-30, -32}, {-40, -8}};
  const float (*pts)[2] = side ? sideBody : rearBody;
  int n = side ? 7 : 6;
  for (int i = 0; i < n; i++) {
    int x0, y0, x1, y1;
    rot(pts[i][0], pts[i][1], x0, y0);
    rot(pts[(i + 1) % n][0], pts[(i + 1) % n][1], x1, y1);
    frame.drawLine(x0, y0, x1, y1, warnCol);
    frame.drawLine(x0, y0 + 1, x1, y1 + 1, warnCol);
  }
  // Wheels
  const float wheelsSide[][2] = {{-35, 22}, {35, 22}};
  const float wheelsRear[][2] = {{-28, 22}, {28, 22}};
  for (int i = 0; i < 2; i++) {
    int wx, wy;
    rot(side ? wheelsSide[i][0] : wheelsRear[i][0], side ? wheelsSide[i][1] : wheelsRear[i][1], wx, wy);
    if (side) frame.fillCircle(wx, wy, 9, C_DIM);
    else frame.fillRect(wx - 6, wy - 9, 12, 18, C_DIM);
  }
  drawDegrees((int)roundf(angleDeg), cx, cy + 62, &fonts::FreeSansBold18pt7b, warnCol, 4);
  frame.setFont(&fonts::Font2);
  frame.setTextColor(C_DIM);
  frame.drawString(label, cx, cy - 68);
}

void drawInclinometer() {
  drawHeader("INCLINE");
  drawVehicle(82, 116, tel.pitch, true, "PITCH (side)", fabsf(tel.pitch) > 25 ? C_RED : fabsf(tel.pitch) > 15 ? C_GOLD : C_GREEN);
  drawVehicle(238, 116, tel.roll, false, "ROLL (rear)", fabsf(tel.roll) > 20 ? C_RED : fabsf(tel.roll) > 12 ? C_GOLD : C_GREEN);
}

void drawSatellites() {
  drawHeader("SATELLITES");
  // Sky plot
  const int cx = 90, cy = 128, R = 84;
  for (int el : {0, 30, 60}) frame.drawCircle(cx, cy, (90 - el) * R / 90, C_GRID);
  frame.drawFastHLine(cx - R, cy, 2 * R, C_GRID);
  frame.drawFastVLine(cx, cy - R, 2 * R, C_GRID);
  frame.setFont(&fonts::Font0);
  frame.setTextColor(C_DIM);
  frame.setTextDatum(middle_center);
  frame.drawString("N", cx, cy - R - 6);
  for (int i = 0; i < satCount; i++) {
    const Sat& s = sats[i];
    if (s.el < 0 || s.az < 0) continue;
    float r = (90 - s.el) * R / 90.0f;
    int x = cx + (int)(r * sinf(s.az * DEG_TO_RAD));
    int y = cy - (int)(r * cosf(s.az * DEG_TO_RAD));
    uint32_t col = snrColour(s.snr);
    if (s.used) frame.fillCircle(x, y, 5, col);
    else frame.drawCircle(x, y, 5, col);
    frame.setTextColor(col);
    char buf[8];
    snprintf(buf, sizeof(buf), "%d", s.prn);
    frame.drawString(buf, x, y - 10);
  }
  // SNR bars (strongest first, up to 12)
  int order[40];
  for (int i = 0; i < satCount; i++) order[i] = i;
  std::sort(order, order + satCount, [](int a, int b) {
    if (sats[a].used != sats[b].used) return sats[a].used;
    return sats[a].snr > sats[b].snr;
  });
  int shown = min(satCount, 12);
  const int bx = 186, by = 40, bh = 150, bw = 10;
  frame.setTextDatum(top_center);
  for (int i = 0; i < shown; i++) {
    const Sat& s = sats[order[i]];
    int x = bx + i * 11;
    int h = (int)(min(1.0f, s.snr / 50.0f) * (bh - 30));
    frame.fillRect(x, by, bw, bh - 30, C_TRACK);
    frame.fillRect(x, by + (bh - 30) - h, bw, h, s.used ? snrColour(s.snr) : C_DIM);
    frame.setTextColor(C_DIM);
    char buf[8];
    snprintf(buf, sizeof(buf), "%d", s.prn);
    frame.setFont(&fonts::TomThumb);
    frame.drawString(buf, x + bw / 2, by + bh - 26);
  }
  frame.setFont(&fonts::Font0);
  frame.setTextDatum(middle_left);
  frame.setTextColor(C_DIM);
  if (!satCount) frame.drawString("Waiting for data...", bx, 120);
  char buf[32];
  snprintf(buf, sizeof(buf), "HDOP %.1f", tel.hdop);
  frame.drawString(buf, bx, 212);
}

void render() {
  frame.fillScreen(C_BG);
  if (page == 0) drawDashboard();
  else if (page == 1) drawInclinometer();
  else drawSatellites();
  drawPageDots();
  frame.pushSprite(0, 0);
}

void showMessage(const char* l1, const char* l2 = "", const char* l3 = "") {
  lcd.fillScreen(C_BG);
  lcd.setTextDatum(middle_center);
  lcd.setFont(&fonts::FreeSansBold12pt7b);
  lcd.setTextColor(C_CYAN);
  lcd.drawString(l1, lcd.width() / 2, 80);
  lcd.setFont(&fonts::FreeSans9pt7b);
  lcd.setTextColor(C_WHITE);
  lcd.drawString(l2, lcd.width() / 2, 125);
  lcd.setTextColor(C_DIM);
  lcd.drawString(l3, lcd.width() / 2, 155);
}

// ---------- Wi-Fi setup ----------
void startWifi(bool forcePortal) {
  WiFiManager wm;
  WiFiManagerParameter hostParam("pihost", "Pi hostname or IP", settings.piHost.c_str(), 40);
  wm.addParameter(&hostParam);
  wm.setConnectTimeout(20);
  wm.setConfigPortalTimeout(300);
  wm.setAPCallback([](WiFiManager*) {
    setLed(false, false, true);
    showMessage("Wi-Fi setup", "Join hotspot: GPS-Display-Setup", "then open 192.168.4.1 if no page appears");
  });
  wm.setSaveParamsCallback([&]() {
    settings.piHost = hostParam.getValue();
    saveSettings();
  });
  if (forcePortal) wm.resetSettings();
  showMessage("Connecting Wi-Fi...", WiFi.SSID().c_str(), "");
  if (!wm.autoConnect("GPS-Display-Setup")) {
    showMessage("No Wi-Fi", "Restarting...", "");
    delay(3000);
    ESP.restart();
  }
  settings.piHost = hostParam.getValue();
  saveSettings();
  MDNS.begin("gps-display");
}

void handleSerial() {
  if (!Serial.available()) return;
  char c = Serial.read();
  bool reinit = false;
  if (c == 'i') { settings.invert = !settings.invert; reinit = true; }
  if (c == 'd') { settings.st7789 = !settings.st7789; saveSettings(); Serial.println("Driver changed, restarting"); delay(200); ESP.restart(); }
  if (c == 'r') { settings.rotation = (settings.rotation + 1) % 8; reinit = true; }
  if (c == 'w') { WiFiManager wm; wm.resetSettings(); Serial.println("Wi-Fi forgotten, restarting"); delay(200); ESP.restart(); }
  if (reinit) {
    saveSettings();
    lcd.invertDisplay(settings.invert);
    lcd.setRotation(settings.rotation);
  }
  if (c == 'i' || c == 'r' || c == 's') {
    Serial.printf("driver=%s invert=%d rotation=%d pihost=%s ip=%s wifi=%s rssi=%d link=%d heap=%u\n",
                  settings.st7789 ? "ST7789" : "ILI9341", settings.invert, settings.rotation,
                  settings.piHost.c_str(), piIP.toString().c_str(), WiFi.SSID().c_str(), WiFi.RSSI(),
                  linkOk(), ESP.getFreeHeap());
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_R, OUTPUT);
  pinMode(LED_G, OUTPUT);
  pinMode(LED_B, OUTPUT);
  setLed(false, false, false);

  loadSettings();
  lcd.setup(settings.st7789, settings.invert);
  lcd.init();
  lcd.setRotation(settings.rotation);
  lcd.setBrightness(255);
  Serial.printf("[LCD] driver=%s invert=%d rotation=%d\n", settings.st7789 ? "ST7789" : "ILI9341", settings.invert, settings.rotation);

  frame.setColorDepth(8);
  if (!frame.createSprite(320, 240)) Serial.println("[LCD] Sprite allocation failed");

  // Colour check shown at boot: the three bars should read red, green, blue.
  lcd.fillScreen(C_BG);
  lcd.fillRect(20, 170, 90, 30, C_PURE_R);
  lcd.fillRect(115, 170, 90, 30, C_PURE_G);
  lcd.fillRect(210, 170, 90, 30, C_PURE_B);
  showMessage("4WD GPS Display", "Hold screen now to reset Wi-Fi", "");
  lcd.fillRect(20, 170, 90, 30, C_PURE_R);
  lcd.fillRect(115, 170, 90, 30, C_PURE_G);
  lcd.fillRect(210, 170, 90, 30, C_PURE_B);

  // Hold the screen ~2 s at boot to clear Wi-Fi settings
  bool held = true;
  for (int i = 0; i < 20; i++) {
    int32_t x, y;
    if (!lcd.getTouch(&x, &y)) held = false;
    delay(100);
  }
  startWifi(held);
  resolvePi();
}

void loop() {
  handleSerial();
  uint32_t now = millis();

  static bool wasTouched = false;
  int32_t tx, ty;
  bool touched = lcd.getTouch(&tx, &ty);
  if (touched && !wasTouched) {
    page = (page + 1) % PAGE_COUNT;
    lastSatsPoll = 0;
  }
  wasTouched = touched;

  static uint32_t lastReconnect = 0;
  if (WiFi.status() != WL_CONNECTED && now - lastReconnect > 5000) {
    lastReconnect = now;
    WiFi.reconnect();
  }
  if (now - lastGpsPoll >= GPS_POLL_MS) {
    lastGpsPoll = now;
    pollGps();
  }
  if (page == 2 && now - lastSatsPoll >= SATS_POLL_MS) {
    lastSatsPoll = now;
    pollSats();
  }

  bool ok = linkOk();
  setLed(!ok, ok, false);

  static uint32_t lastDraw = 0;
  if (now - lastDraw >= 100) {
    lastDraw = now;
    render();
  }
  delay(5);
}
