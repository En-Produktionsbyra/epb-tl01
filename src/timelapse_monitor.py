#!/usr/bin/env python3

import gphoto2 as gp
import time
import os
import logging
from datetime import datetime
import serial
import RPi.GPIO as GPIO
from onedrivesdk import get_default_client, AuthProvider
import json
import requests
import sqlite3
import hashlib
import psutil
import threading
from pathlib import Path
from dotenv import load_dotenv
import subprocess

class NotificationPriority:
    CRITICAL = 5    # System down, critical failures
    HIGH = 4        # Power issues, storage critical
    MEDIUM = 3      # Upload failures, camera issues
    LOW = 2         # Recovery events, disk warnings
    INFO = 1        # Self-healing events

class TimelapseMonitor:
    def __init__(self):
        # Load environment variables
        env_path = Path("/opt/timelapse/config/.env")
        if not env_path.exists():
            raise RuntimeError("Configuration file not found: /opt/timelapse/config/.env")
        load_dotenv(env_path)
        
        # Get required environment variables
        required_vars = [
            'ONEDRIVE_CLIENT_ID',
            'ONEDRIVE_CLIENT_SECRET',
            'NTFY_TOPIC'
        ]
        missing_vars = [var for var in required_vars if not os.getenv(var)]
        if missing_vars:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing_vars)}")
            
        # Initialize logging
        logging.basicConfig(
            filename='/opt/timelapse/logs/timelapse_monitor.log',
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        
        # Camera monitoring settings
        self.camera = None
        self.processed_files = set()
        self.check_interval = int(os.getenv('CHECK_INTERVAL', '60'))
        self.connect_retries = 3
        self.consecutive_failures = 0
        self.max_consecutive_failures = 3
        
        # Local storage for temporary files
        self.temp_dir = Path("/tmp/timelapse_monitor")
        self.backup_dir = Path("/opt/timelapse/backup")
        self.temp_dir.mkdir(exist_ok=True)
        self.backup_dir.mkdir(exist_ok=True)
        
        # Database path
        self.db_path = Path("/opt/timelapse/timelapse_monitor.db")
        
        # 4G Modem settings
        self.modem_serial = serial.Serial('/dev/ttyUSB0', 115200, timeout=1)
        
        # OneDrive settings
        self.client_id = os.getenv('ONEDRIVE_CLIENT_ID')
        self.client_secret = os.getenv('ONEDRIVE_CLIENT_SECRET')
        self.scopes = ['wl.signin', 'wl.offline_access', 'onedrive.readwrite']
        
        # Notification settings
        self.ntfy_topic = os.getenv('NTFY_TOPIC')
        self.ntfy_url = f"https://ntfy.sh/{self.ntfy_topic}"
        
        # UPS monitoring pins
        self.UPS_PIN = 18  # GPIO pin for UPS status
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.UPS_PIN, GPIO.IN)
        
        # Initialize components
        self.init_4g()
        self.init_onedrive()
        
        # Start monitoring thread
        self.monitor_thread = threading.Thread(target=self.system_monitor, daemon=True)
        self.monitor_thread.start()

    def connect_camera(self):
        """Attempt to connect to the camera with retries"""
        for attempt in range(self.connect_retries):
            try:
                # Reset USB connection if needed
                if attempt > 0:
                    subprocess.run(['sudo', 'usbreset', '0x04a9'], 
                                 capture_output=True, text=True)
                    time.sleep(2)
                
                # Initialize camera connection
                self.camera = gp.Camera()
                self.camera.init()
                logging.info("Camera connected successfully")
                return True
                
            except Exception as e:
                logging.warning(f"Camera connection attempt {attempt + 1} failed: {str(e)}")
                if self.camera:
                    try:
                        self.camera.exit()
                    except:
                        pass
                    self.camera = None
                time.sleep(2)
        
        logging.error("Failed to connect to camera after all retries")
        self.send_notification("Camera connection failed", NotificationPriority.HIGH)
        return False

    def init_onedrive(self):
        """Initialize OneDrive connection with token refresh handling"""
        try:
            self.auth = AuthProvider(
                self.client_id,
                self.client_secret,
                self.scopes
            )
            self.client = get_default_client(self.auth)
            
            # Test connection and token
            self.client.drive.get()
            logging.info("OneDrive initialized successfully")
        except Exception as e:
            if "token expired" in str(e).lower():
                try:
                    self.auth.refresh_token()
                    self.client = get_default_client(self.auth)
                    logging.info("OneDrive token refreshed successfully")
                except Exception as refresh_error:
                    logging.error(f"OneDrive token refresh failed: {refresh_error}")
                    self.send_notification("OneDrive authentication failed", NotificationPriority.HIGH)
            else:
                logging.error(f"OneDrive initialization failed: {e}")
                self.send_notification("OneDrive initialization failed", NotificationPriority.HIGH)

    def init_4g(self):
        """Initialize the 4G modem"""
        try:
            commands = [
                'AT',
                'AT+CPIN?',
                'AT+CSQ',
                'AT+CREG?',
                'AT+CGACT=1,1'
            ]
            
            for cmd in commands:
                self.modem_serial.write((cmd + '\r\n').encode())
                time.sleep(1)
                response = self.modem_serial.read_all().decode()
                logging.info(f"Modem response to {cmd}: {response}")
                
            logging.info("4G modem initialized successfully")
        except Exception as e:
            logging.error(f"4G modem initialization failed: {str(e)}")
            self.send_notification("4G modem initialization failed", NotificationPriority.HIGH)

    def check_network(self):
        """Check network connectivity with retry"""
        retry_count = 0
        max_retries = 3
        retry_delay = 5
        
        while retry_count < max_retries:
            try:
                # Try both IPv4 and IPv6
                requests.get("https://1.1.1.1", timeout=5)
                return True
            except requests.exceptions.RequestException:
                retry_count += 1
                if retry_count < max_retries:
                    time.sleep(retry_delay * (2 ** (retry_count - 1)))  # Exponential backoff
        
        return False

    def wait_for_network(self, timeout=300):
        """Wait for network connectivity with timeout"""
        start_time = time.time()
        while (time.time() - start_time) < timeout:
            if self.check_network():
                return True
            time.sleep(10)
        return False

    def get_camera_files(self):
        """Get list of files from camera with connection handling"""
        try:
            if not self.camera and not self.connect_camera():
                return []
            
            file_list = self.camera.folder_list_files('/')
            return [(f.name, f) for f in file_list]
            
        except gp.GPhoto2Error as gp_error:
            logging.error(f"GPhoto2 error: {str(gp_error)}")
            if "Camera is already in use" in str(gp_error):
                # Handle busy camera - likely taking a photo
                return []
            
            # For other errors, reset connection
            if self.camera:
                try:
                    self.camera.exit()
                except:
                    pass
                self.camera = None
            return []
            
        except Exception as e:
            logging.error(f"Error listing camera files: {str(e)}")
            return []

    def download_file(self, file_info):
        """Download a single file from the camera"""
        filename, camera_file = file_info
        local_path = self.temp_dir / f"timelapse_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{filename}"
        
        try:
            if not self.camera and not self.connect_camera():
                return None
                
            camera_file = self.camera.file_get(
                '/',
                filename,
                gp.GP_FILE_TYPE_NORMAL
            )
            camera_file.save(str(local_path))
            return local_path
            
        except Exception as e:
            logging.error(f"Failed to download file {filename}: {str(e)}")
            return None

    def calculate_checksum(self, file_path):
        """Calculate SHA-256 checksum of file"""
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()

    def verify_file(self, file_path, original_size=None):
        """Verify file integrity"""
        if not file_path.exists():
            return False
            
        current_size = file_path.stat().st_size
        if original_size and current_size != original_size:
            return False
            
        return True

    def handle_file_processing(self, filename, local_path):
        """Handle file processing with database tracking"""
        try:
            checksum = self.calculate_checksum(local_path)
            file_size = local_path.stat().st_size
            
            with sqlite3.connect(self.db_path) as conn:
                # Check if file was already processed
                cursor = conn.execute(
                    "SELECT filename FROM processed_files WHERE filename = ? AND upload_status = 'success'",
                    (filename,)
                )
                if cursor.fetchone():
                    return True
                
                # Store file info
                conn.execute(
                    """
                    INSERT OR REPLACE INTO processed_files 
                    (filename, checksum, size, processed_at, upload_status, retries)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (filename, checksum, file_size, datetime.now(), 'pending', 0)
                )
            
            # Try OneDrive upload
            if self.upload_to_onedrive(local_path):
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute(
                        "UPDATE processed_files SET upload_status = 'success' WHERE filename = ?",
                        (filename,)
                    )
                return True
            
            # If OneDrive fails, save to backup location
            backup_path = self.backup_dir / filename
            local_path.rename(backup_path)
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE processed_files SET upload_status = 'backup' WHERE filename = ?",
                    (filename,)
                )
            
            self.send_notification(
                f"File {filename} saved to backup storage after upload failure",
                NotificationPriority.MEDIUM
            )
            return True
            
        except Exception as e:
            self.send_notification(
                f"File processing failed for {filename}: {str(e)}",
                NotificationPriority.HIGH
            )
            return False

    def upload_to_onedrive(self, filepath):
        """Upload file to OneDrive with proper cleanup"""
        temp_files = []
        try:
            filename = filepath.name
            
            # Create temporary copy for upload
            temp_path = self.temp_dir / f"upload_{filename}"
            temp_files.append(temp_path)
            
            with open(filepath, 'rb') as src:
                with open(temp_path, 'wb') as dst:
                    dst.write(src.read())
            
            # Verify copy
            if not self.verify_file(temp_path, filepath.stat().st_size):
                raise Exception("File verification failed after copy")
            
            # Attempt upload
            with open(temp_path, 'rb') as file:
                self.client.item(drive='me', path=f'/Timelapse/{filename}').upload(file)
            
            logging.info(f"File uploaded to OneDrive: {filename}")
            
            # Clean up original file after successful upload
            filepath.unlink()
            return True
            
        except Exception as e:
            logging.error(f"OneDrive upload failed: {str(e)}")
            self.send_notification(f"OneDrive upload failed: {str(e)}", NotificationPriority.MEDIUM)
            
            # Move to backup location on failure
            try:
                backup_path = self.backup_dir / filepath.name
                filepath.rename(backup_path)
                logging.info(f"File moved to backup: {backup_path}")
            except Exception as backup_error:
                logging.error(f"Failed to move file to backup: {str(backup_error)}")
            
            return False
            
        finally:
            # Clean up any temporary files
            for temp_file in temp_files:
                try:
                    if temp_file.exists():
                        temp_file.unlink()
                except Exception as cleanup_error:
                    logging.error(f"Failed to clean up temporary file: {str(cleanup_error)}")

    def send_notification(self, message, priority):
        """Send notification with fallback options"""
        try:
            # First try ntfy.sh
            headers = {
                "Priority": str(priority),
                "Title": "Timelapse Monitor Alert",
                "Tags": "warning"
            }
            
            response = requests.post(
                self.ntfy_url,
                data=message.encode(encoding='utf-8'),
                headers=headers,
                timeout=10  # Add timeout
            )
            response.raise_for_status()
            return True
        except Exception as e:
            logging.error(f"ntfy.sh notification failed: {e}")
            
            try:
                # Fallback to 4G modem direct SMS
                # AT commands for SMS
                commands = [
                    f'AT+CMGF=1\r',  # Text mode
                    f'AT+CMGS="{os.getenv("ADMIN_PHONE")}"\r',  # Phone number from env
                    f'{message}\x1A'  # Message content + CTRL+Z
                ]
                
                for cmd in commands:
                    self.modem_serial.write(cmd.encode())
                    time.sleep(1)
                    response = self.modem_serial.read_all().decode()
                    if "ERROR" in response:
                        raise Exception(f"Modem command failed: {response}")
                
                logging.info("Notification sent via SMS fallback")
                return True
            except Exception as sms_error:
                logging.error(f"SMS notification failed: {sms_error}")
                return False

    def monitor_power(self):
        """Monitor UPS power status"""
        if GPIO.input(self.UPS_PIN) == GPIO.LOW:
            logging.warning("Power failure detected!")
            self.send_notification("Power failure detected!", NotificationPriority.HIGH)
            return False
        return True

    def check_new_images(self):
        """Check for new images and process them"""
        try:
            current_files = set(name for name, _ in self.get_camera_files())
            new_files = current_files - self.processed_files
            
            if new_files:
                logging.info(f"Found {len(new_files)} new images")
                
                # Get full file info for new files
                all_files = self.get_camera_files()
                new_file_infos = [f for f in all_files if f[0] in new_files]
                
                for file_info in new_file_infos:
                    local_path = self.download_file(file_info)
                    if local_path:
                        if self.handle_file_processing(file_info[0], local_path):
                            self.processed_files.add(file_info[0])
                
                # Limit size of processed files set
                if len(self.processed_files) > 1000:
                    self.processed_files = set(list(self.processed_files)[-1000:])
                    
        except Exception as e:
            logging.error(f"Failed to check new images: {str(e)}")
            self.send_notification("Failed to check new images", NotificationPriority.HIGH)

    def process_backup_files(self):
        """Try to upload files from backup storage"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT filename, retries FROM processed_files WHERE upload_status = 'backup'"
                )
                backup_files = cursor.fetchall()
            
            for filename, retries in backup_files:
                if retries >= int(os.getenv('MAX_RETRIES', '5')):
                    continue
                    
                backup_path = self.backup_dir / filename
                if not backup_path.exists():
                    continue
                
                if self.upload_to_onedrive(backup_path):
                    with sqlite3.connect(self.db_path) as conn:
                        conn.execute(
                            "UPDATE processed_files SET upload_status = 'success' WHERE filename = ?",
                            (filename,)
                        )
                    self.send_notification(
                        f"Successfully uploaded backup file {filename}",
                        NotificationPriority.LOW
                    )
                else:
                    with sqlite3.connect(self.db_path) as conn:
                        conn.execute(
                            "UPDATE processed_files SET retries = retries + 1 WHERE filename = ?",
                            (filename,)
                        )
                    
        except Exception as e:
            logging.error(f"Error processing backup files: {str(e)}")
            self.send_notification(
                f"Error processing backup files: {str(e)}",
                NotificationPriority.HIGH
            )

    def system_monitor(self):
        """Monitor system health in separate thread"""
        while True:
            try:
                # Check disk space
                disk_usage = psutil.disk_usage('/')
                if disk_usage.percent >= int(os.getenv('DISK_CRITICAL_THRESHOLD', '10')):
                    self.send_notification(
                        f"Critical: Disk space low ({disk_usage.percent}% used)",
                        NotificationPriority.CRITICAL
                    )
                elif disk_usage.percent >= int(os.getenv('DISK_WARNING_THRESHOLD', '25')):
                    self.send_notification(
                        f"Warning: Disk space getting low ({disk_usage.percent}% used)",
                        NotificationPriority.LOW
                    )
                
                # Check CPU temperature
                try:
                    with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
                        temp = float(f.read().strip()) / 1000
                        if temp >= int(os.getenv('TEMP_CRITICAL_THRESHOLD', '80')):
                            self.send_notification(
                                f"Critical: System temperature {temp}°C",
                                NotificationPriority.CRITICAL
                            )
                        elif temp >= int(os.getenv('TEMP_WARNING_THRESHOLD', '70')):
                            self.send_notification(
                                f"Warning: System temperature {temp}°C",
                                NotificationPriority.HIGH
                            )
                except:
                    pass  # Temperature reading not critical
                
                time.sleep(300)  # Check every 5 minutes
                
            except Exception as e:
                logging.error(f"System monitor error: {e}")
                time.sleep(60)  # Retry after 1 minute

    def run(self):
        """Main monitoring loop"""
        logging.info("Starting timelapse monitoring")
        self.send_notification("Timelapse monitoring started", NotificationPriority.INFO)
        
        while True:
            try:
                # Check power status
                if not self.monitor_power():
                    self.consecutive_failures += 1
                else:
                    self.consecutive_failures = 0
                
                # Check for new images
                self.check_new_images()
                
                # Try to process any backup files
                self.process_backup_files()
                
                # Disconnect camera between checks to allow sleep
                if self.camera:
                    try:
                        self.camera.exit()
                    except:
                        pass
                    self.camera = None
                
                # Check if we need to restart due to too many failures
                if self.consecutive_failures >= self.max_consecutive_failures:
                    self.send_notification(
                        f"System restart triggered after {self.consecutive_failures} consecutive failures",
                        NotificationPriority.CRITICAL
                    )
                    subprocess.run(['sudo', 'reboot'])
                
                time.sleep(self.check_interval)
                
            except KeyboardInterrupt:
                self.send_notification("Monitoring stopped by user", NotificationPriority.HIGH)
                break
            except Exception as e:
                self.send_notification(f"Main loop error: {str(e)}", NotificationPriority.CRITICAL)
                self.consecutive_failures += 1
                time.sleep(60)

    def cleanup(self):
        """Cleanup resources"""
        try:
            if self.camera:
                self.camera.exit()
            GPIO.cleanup()
            self.modem_serial.close()
            self.send_notification("System cleanup completed", NotificationPriority.INFO)
        except Exception as e:
            self.send_notification(f"Cleanup failed: {str(e)}", NotificationPriority.HIGH)

if __name__ == "__main__":
    monitor = TimelapseMonitor()
    try:
        monitor.run()
    finally:
        monitor.cleanup()