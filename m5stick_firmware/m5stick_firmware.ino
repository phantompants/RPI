/*
  M5StickC Plus2 IMU Telemetry Streamer for RPi 4WD GPS Tracker
  Reads onboard MPU6886 IMU (Accel + Gyro) at 50Hz
  Outputs JSON to USB Serial & displays Pitch/Roll G-Force on screen

  Datum (zero) position:
  - Park level, then HOLD the big front button (A) for 1 s: the current orientation becomes
    "level" (0 deg pitch / 0 deg roll). Works however the stick is mounted.
  - HOLD the side button (B) for 3 s to clear the datum (back to raw sensor axes).
  - The datum is saved in flash, so it survives power cycles.
  All values sent to the Pi (accel and gyro) are rotated into the datum frame.
*/

#include <M5StickCPlus2.h>
#include <Preferences.h>

float ax = 0, ay = 0, az = 0;
float gx = 0, gy = 0, gz = 0;
unsigned long lastUpdate = 0;

// Datum: rotation matrix taking the parked gravity direction to straight down the Z axis
Preferences prefs;
bool datumSet = false;
float R[3][3] = {{1, 0, 0}, {0, 1, 0}, {0, 0, 1}};

void rotate(float& x, float& y, float& z) {
  float rx = R[0][0] * x + R[0][1] * y + R[0][2] * z;
  float ry = R[1][0] * x + R[1][1] * y + R[1][2] * z;
  float rz = R[2][0] * x + R[2][1] * y + R[2][2] * z;
  x = rx; y = ry; z = rz;
}

// Smallest rotation taking unit vector g onto (0,0,1) (Rodrigues' formula)
void buildDatum(float gxv, float gyv, float gzv) {
  float n = sqrtf(gxv * gxv + gyv * gyv + gzv * gzv);
  if (n < 0.5f) return;  // not a sensible gravity reading
  gxv /= n; gyv /= n; gzv /= n;
  // axis k = g x z = (gy, -gx, 0); cos = g.z = gz; sin = |k|
  float kx = gyv, ky = -gxv;
  float s = sqrtf(kx * kx + ky * ky);
  float c = gzv;
  if (s < 1e-6f) {
    // Already aligned (or upside down: rotate 180 deg about X)
    float sign = c > 0 ? 1.0f : -1.0f;
    float I[3][3] = {{1, 0, 0}, {0, sign, 0}, {0, 0, sign}};
    memcpy(R, I, sizeof(R));
    return;
  }
  kx /= s; ky /= s;
  float v = 1 - c;
  R[0][0] = c + kx * kx * v;  R[0][1] = kx * ky * v;      R[0][2] = ky * s;
  R[1][0] = ky * kx * v;      R[1][1] = c + ky * ky * v;  R[1][2] = -kx * s;
  R[2][0] = -ky * s;          R[2][1] = kx * s;           R[2][2] = c;
}

void loadDatum() {
  prefs.begin("imu", true);
  datumSet = prefs.getBool("set", false);
  if (datumSet) buildDatum(prefs.getFloat("gx", 0), prefs.getFloat("gy", 0), prefs.getFloat("gz", 1));
  prefs.end();
}

void setDatum() {
  // Average 0.5 s of accelerometer readings so vibration doesn't skew the datum
  float sx = 0, sy = 0, sz = 0;
  const int samples = 25;
  for (int i = 0; i < samples; i++) {
    float x, y, z;
    StickCP2.Imu.getAccelData(&x, &y, &z);
    sx += x; sy += y; sz += z;
    delay(20);
  }
  sx /= samples; sy /= samples; sz /= samples;
  buildDatum(sx, sy, sz);
  datumSet = true;
  prefs.begin("imu", false);
  prefs.putBool("set", true);
  prefs.putFloat("gx", sx);
  prefs.putFloat("gy", sy);
  prefs.putFloat("gz", sz);
  prefs.end();
}

void clearDatum() {
  float I[3][3] = {{1, 0, 0}, {0, 1, 0}, {0, 0, 1}};
  memcpy(R, I, sizeof(R));
  datumSet = false;
  prefs.begin("imu", false);
  prefs.clear();
  prefs.end();
}

void showBanner(const char* text, uint16_t colour) {
  StickCP2.Display.fillScreen(BLACK);
  StickCP2.Display.setTextColor(colour, BLACK);
  StickCP2.Display.setCursor(10, 50);
  StickCP2.Display.setTextSize(3);
  StickCP2.Display.print(text);
  StickCP2.Display.setTextSize(2);
  delay(1200);
  StickCP2.Display.fillScreen(BLACK);
}

void drawTitle() {
  StickCP2.Display.setTextColor(WHITE, BLACK);
  StickCP2.Display.setCursor(10, 10);
  StickCP2.Display.printf("4WD IMU LOG ");
  StickCP2.Display.setTextColor(datumSet ? GREEN : DARKGREY, BLACK);
  StickCP2.Display.printf(datumSet ? "DATUM " : "RAW   ");
}

void setup() {
  auto cfg = M5.config();
  StickCP2.begin(cfg);

  Serial.begin(115200);

  StickCP2.Display.setRotation(1); // Landscape mode
  StickCP2.Display.fillScreen(BLACK);
  StickCP2.Display.setTextSize(2);
  loadDatum();
  drawTitle();
}

void loop() {
  StickCP2.update();

  // Big front button held 1 s: set datum. Side button held 3 s: clear it.
  static bool aHandled = false, bHandled = false;
  if (StickCP2.BtnA.pressedFor(1000) && !aHandled) {
    aHandled = true;
    showBanner("HOLD STILL", YELLOW);
    setDatum();
    showBanner("DATUM SET", GREEN);
    drawTitle();
  }
  if (StickCP2.BtnA.isReleased()) aHandled = false;
  if (StickCP2.BtnB.pressedFor(3000) && !bHandled) {
    bHandled = true;
    clearDatum();
    showBanner("CLEARED", RED);
    drawTitle();
  }
  if (StickCP2.BtnB.isReleased()) bHandled = false;

  // 50Hz Sampling Rate (Every 20ms)
  if (millis() - lastUpdate >= 20) {
    lastUpdate = millis();

    // Read IMU values from onboard MPU6886, rotated into the datum frame
    StickCP2.Imu.getAccelData(&ax, &ay, &az);
    StickCP2.Imu.getGyroData(&gx, &gy, &gz);
    rotate(ax, ay, az);
    rotate(gx, gy, gz);

    // 1. Output compact JSON over USB Serial to Raspberry Pi
    Serial.printf("{\"ax\":%.3f,\"ay\":%.3f,\"az\":%.3f,\"gx\":%.2f,\"gy\":%.2f,\"gz\":%.2f,\"datum\":%d}\n",
                  ax, ay, az, gx, gy, gz, datumSet ? 1 : 0);

    // 2. Refresh Onscreen Display Gauges (every 100ms)
    static unsigned long lastDisp = 0;
    if (millis() - lastDisp >= 100) {
      lastDisp = millis();

      float pitch = atan2(ax, sqrt(ay*ay + az*az)) * 180.0 / PI;
      float roll  = atan2(ay, sqrt(ax*ax + az*az)) * 180.0 / PI;
      float g_total = sqrt(ax*ax + ay*ay + az*az);

      StickCP2.Display.setCursor(10, 35);
      StickCP2.Display.setTextColor(CYAN, BLACK);
      StickCP2.Display.printf("Pitch: %5.1f deg  ", pitch);

      StickCP2.Display.setCursor(10, 60);
      StickCP2.Display.setTextColor(YELLOW, BLACK);
      StickCP2.Display.printf("Roll : %5.1f deg  ", roll);

      StickCP2.Display.setCursor(10, 85);
      if (g_total > 1.3 || g_total < 0.7) {
        StickCP2.Display.setTextColor(RED, BLACK); // High corrugation / bump
      } else {
        StickCP2.Display.setTextColor(GREEN, BLACK);
      }
      StickCP2.Display.printf("G-Force: %4.2f g  ", g_total);

      StickCP2.Display.setCursor(10, 112);
      StickCP2.Display.setTextColor(DARKGREY, BLACK);
      StickCP2.Display.setTextSize(1);
      StickCP2.Display.printf("Hold A 1s: set level  Hold B 3s: clear");
      StickCP2.Display.setTextSize(2);
    }
  }
}
