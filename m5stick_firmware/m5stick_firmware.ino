/*
  M5StickC Plus2 IMU Telemetry Streamer for RPi 4WD GPS Tracker
  Reads onboard MPU6886 IMU (Accel + Gyro) at 50Hz
  Outputs JSON to USB Serial & displays Pitch/Roll G-Force on screen
*/

#include <M5StickCPlus2.h>

float ax = 0, ay = 0, az = 0;
float gx = 0, gy = 0, gz = 0;
unsigned long lastUpdate = 0;

void setup() {
  auto cfg = M5.config();
  StickCP2.begin(cfg);
  
  Serial.begin(115200);
  
  StickCP2.Display.setRotation(1); // Landscape mode
  StickCP2.Display.fillScreen(BLACK);
  StickCP2.Display.setTextColor(WHITE, BLACK);
  StickCP2.Display.setTextSize(2);
  StickCP2.Display.setCursor(10, 10);
  StickCP2.Display.printf("4WD IMU LOG");
}

void loop() {
  StickCP2.update();

  // 50Hz Sampling Rate (Every 20ms)
  if (millis() - lastUpdate >= 20) {
    lastUpdate = millis();

    // Read IMU values from onboard MPU6886
    StickCP2.Imu.getAccelData(&ax, &ay, &az);
    StickCP2.Imu.getGyroData(&gx, &gy, &gz);

    // 1. Output compact JSON over USB Serial to Raspberry Pi
    Serial.printf("{\"ax\":%.3f,\"ay\":%.3f,\"az\":%.3f,\"gx\":%.2f,\"gy\":%.2f,\"gz\":%.2f}\n",
                  ax, ay, az, gx, gy, gz);

    // 2. Refresh Onscreen Display Gauges (every 100ms)
    static unsigned long lastDisp = 0;
    if (millis() - lastDisp >= 100) {
      lastDisp = millis();

      float pitch = atan2(ax, sqrt(ay*ay + az*az)) * 180.0 / PI;
      float roll  = atan2(ay, sqrt(ax*ax + az*az)) * 180.0 / PI;
      float g_total = sqrt(ax*ax + ay*ay + az*az);

      StickCP2.Display.setCursor(10, 35);
      StickCP2.Display.setTextColor(CYAN, BLACK);
      StickCP2.Display.printf("Pitch: %4.1f deg  ", pitch);

      StickCP2.Display.setCursor(10, 60);
      StickCP2.Display.setTextColor(YELLOW, BLACK);
      StickCP2.Display.printf("Roll : %4.1f deg  ", roll);

      StickCP2.Display.setCursor(10, 85);
      if (g_total > 1.3 || g_total < 0.7) {
        StickCP2.Display.setTextColor(RED, BLACK); // High corrugation / bump
      } else {
        StickCP2.Display.setTextColor(GREEN, BLACK);
      }
      StickCP2.Display.printf("G-Force: %4.2f g  ", g_total);
    }
  }
}
