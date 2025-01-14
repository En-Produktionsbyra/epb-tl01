# Exit on any error
set -e

# Minimum required disk space in MB
MIN_DISK_SPACE=500

# Text colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Logger function
log() {
    echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"
}

error() {
    echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')] ERROR:${NC} $1"
}

warning() {
    echo -e "${YELLOW}[$(date +'%Y-%m-%d %H:%M:%S')] WARNING:${NC} $1"
}

# Check if script is run as root
if [ "$EUID" -ne 0 ]; then 
    error "Please run as root (use sudo)"
    exit 1
fi

# Create installation log
INSTALL_LOG="/var/log/timelapse_install.log"
exec 1> >(tee -a "$INSTALL_LOG") 2>&1

# Check available disk space
AVAILABLE_SPACE=$(df -m / | awk 'NR==2 {print $4}')
if [ "$AVAILABLE_SPACE" -lt "$MIN_DISK_SPACE" ]; then
    error "Insufficient disk space. Need at least ${MIN_DISK_SPACE}MB, have ${AVAILABLE_SPACE}MB"
    exit 1
fi

# Check if pi user exists, create if not
if ! id "pi" &>/dev/null; then
    log "Creating pi user..."
    useradd -m -G sudo,video,gpio pi
    echo "pi:raspberry" | chpasswd
    warning "Created default pi user with password 'raspberry'. Please change this!"
fi

# Create installation directory
INSTALL_DIR="/opt/timelapse"
log "Creating installation directory at $INSTALL_DIR"

# Backup existing installation if found
if [ -d "$INSTALL_DIR" ]; then
    BACKUP_TIME=$(date +%Y%m%d_%H%M%S)
    BACKUP_DIR="${INSTALL_DIR}_backup_${BACKUP_TIME}"
    log "Backing up existing installation to ${BACKUP_DIR}"
    cp -r "$INSTALL_DIR" "$BACKUP_DIR"
fi

mkdir -p "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/backup"
mkdir -p "$INSTALL_DIR/logs"

# Install system dependencies
log "Installing system dependencies..."
apt-get update
apt-get install -y \
    python3-pip \
    gphoto2 \
    python3-gphoto2 \
    libusb-dev \
    sqlite3 \
    git \
    build-essential \
    python3-dev \
    sudo \
    usbutils \
    ntpdate \
    watchdog

# Check Python version
log "Checking Python version..."
PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
PYTHON_MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
PYTHON_MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 7 ]); then
    error "Python 3.7 or higher is required. Found version $PYTHON_VERSION"
    exit 1
fi

# Compile and install usbreset utility
log "Installing USB reset utility..."
cat > /tmp/usbreset.c << 'EOF'
#include <stdio.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/ioctl.h>
#include <linux/usbdevice_fs.h>

int main(int argc, char **argv)
{
    const char *filename;
    int fd;
    int rc;

    if (argc != 2) {
        fprintf(stderr, "Usage: usbreset device-filename\n");
        return 1;
    }
    filename = argv[1];

    fd = open(filename, O_WRONLY);
    if (fd < 0) {
        perror("Error opening output file");
        return 1;
    }

    printf("Resetting USB device %s\n", filename);
    rc = ioctl(fd, USBDEVFS_RESET, 0);
    if (rc < 0) {
        perror("Error in ioctl");
        return 1;
    }
    printf("Reset successful\n");

    close(fd);
    return 0;
}
EOF

gcc -o /usr/local/bin/usbreset /tmp/usbreset.c
chmod +x /usr/local/bin/usbreset
rm /tmp/usbreset.c

# Install Python dependencies
log "Installing Python packages..."
PIP_LOG=$(mktemp)
if ! pip3 install \
    onedrivesdk \
    pyserial \
    RPi.GPIO \
    psutil \
    requests \
    pillow \
    python-dotenv \
    2>&1 | tee "$PIP_LOG"; then
    
    error "Python package installation failed. Check $PIP_LOG for details"
    exit 1
fi
rm "$PIP_LOG"

# Interactive configuration
log "Starting interactive configuration..."

# OneDrive configuration
echo -e "\n${GREEN}OneDrive Configuration${NC}"
read -p "Enter your OneDrive Client ID: " ONEDRIVE_CLIENT_ID
read -p "Enter your OneDrive Client Secret: " ONEDRIVE_CLIENT_SECRET

# ntfy.sh configuration
echo -e "\n${GREEN}ntfy.sh Configuration${NC}"
read -p "Enter your ntfy.sh topic name: " NTFY_TOPIC

# System configuration
echo -e "\n${GREEN}System Configuration${NC}"
read -p "Check interval in seconds [60]: " CHECK_INTERVAL
CHECK_INTERVAL=${CHECK_INTERVAL:-60}

read -p "Disk space warning threshold % [25]: " DISK_WARNING
DISK_WARNING=${DISK_WARNING:-25}

read -p "Disk space critical threshold % [10]: " DISK_CRITICAL
DISK_CRITICAL=${DISK_CRITICAL:-10}

read -p "Temperature warning threshold °C [70]: " TEMP_WARNING
TEMP_WARNING=${TEMP_WARNING:-70}

read -p "Temperature critical threshold °C [80]: " TEMP_CRITICAL
TEMP_CRITICAL=${TEMP_CRITICAL:-80}

read -p "Maximum retry attempts [5]: " MAX_RETRIES
MAX_RETRIES=${MAX_RETRIES:-5}

# Create configuration directory and .env file
log "Setting up configuration..."
mkdir -p "$INSTALL_DIR/config"
cat > "$INSTALL_DIR/config/.env" << EOF
# OneDrive Configuration
ONEDRIVE_CLIENT_ID=$ONEDRIVE_CLIENT_ID
ONEDRIVE_CLIENT_SECRET=$ONEDRIVE_CLIENT_SECRET

# ntfy.sh Configuration
NTFY_TOPIC=$NTFY_TOPIC

# System Configuration
CHECK_INTERVAL=$CHECK_INTERVAL
DISK_WARNING_THRESHOLD=$DISK_WARNING
DISK_CRITICAL_THRESHOLD=$DISK_CRITICAL
TEMP_WARNING_THRESHOLD=$TEMP_WARNING
TEMP_CRITICAL_THRESHOLD=$TEMP_CRITICAL
MAX_RETRIES=$MAX_RETRIES
EOF

# Configure USB and device access
log "Setting up device access rules..."

# Camera USB rules
cat > /etc/udev/rules.d/51-camera.rules << EOF
ACTION=="add", ATTRS{idVendor}=="04a9", GROUP="plugdev", MODE="0666"
EOF

# Add pi user to required groups
usermod -a -G dialout,gpio,i2c,plugdev,video pi

# Reload udev rules
udevadm control --reload-rules
udevadm trigger

# Check for watchdog support
log "Configuring watchdog..."
if ! modprobe bcm2835_wdt; then
    warning "Could not load watchdog kernel module"
fi

# Configure watchdog
cat > /etc/watchdog.conf << EOF
watchdog-device = /dev/watchdog
watchdog-timeout = 15
interval = 10
realtime = yes
priority = 1

# Monitor critical processes
pidfile = /var/run/timelapse.pid

# File system checks
file = /var/log
change = 1407

# Temperature monitoring
temperature-sensor = /sys/class/thermal/thermal_zone0/temp
max-temperature = 80000
EOF

# Setup systemd service
log "Creating systemd service..."
cat > /etc/systemd/system/timelapse.service << EOF
[Unit]
Description=Timelapse Monitor Service
After=network.target

[Service]
ExecStart=/usr/bin/python3 $INSTALL_DIR/timelapse_monitor.py
WorkingDirectory=$INSTALL_DIR
User=pi
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# Configure network wait
log "Configuring network wait on boot..."
raspi-config nonint do_wifi_country US  # Change US to your country code
raspi-config nonint do_boot_wait 0

# Create network wait service
cat > /etc/systemd/system/wait-for-network.service << EOF
[Unit]
Description=Wait for Network
Before=timelapse.service
After=network.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'until ping -c1 8.8.8.8 >/dev/null 2>&1; do sleep 1; done'
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
EOF

# Update timelapse service to depend on network
sed -i '/After=network.target/c\After=wait-for-network.service' /etc/systemd/system/timelapse.service

# Setup log rotation
log "Configuring log rotation..."
cat > /etc/logrotate.d/timelapse << EOF
$INSTALL_DIR/logs/timelapse_monitor.log {
    rotate 7
    daily
    compress
    missingok
    notifempty
    create 644 pi pi
}
EOF

# Configure log rotation for installation log
cat > /etc/logrotate.d/timelapse_install << EOF
/var/log/timelapse_install.log {
    rotate 5
    weekly
    compress
    missingok
    notifempty
}
EOF

# Setup file system for power safety
log "Configuring file system for power safety..."
if ! grep -q "defaults,noatime,commit=60" /etc/fstab; then
    cp /etc/fstab /etc/fstab.backup
    sed -i 's/ defaults / defaults,noatime,commit=60 /' /etc/fstab
    warning "File system mount options updated. A reboot is recommended."
fi

# Create recovery script
log "Creating recovery script..."
cat > "$INSTALL_DIR/recover.sh" << 'EOF'
#!/bin/bash
# Recovery script for timelapse system

# Reset USB devices
echo "Resetting USB devices..."
for device in /sys/bus/usb/devices/*/authorized; do
    echo 0 > $device
    sleep 1
    echo 1 > $device
done

# Restart services
echo "Restarting services..."
systemctl restart timelapse
systemctl restart watchdog

# Check system status
echo "System status:"
systemctl status timelapse
systemctl status watchdog
EOF

chmod +x "$INSTALL_DIR/recover.sh"

# Set correct permissions
log "Setting permissions..."
chown -R pi:pi "$INSTALL_DIR"
chmod -R 755 "$INSTALL_DIR"

# Test device access
log "Testing device access..."
if ! groups pi | grep -q "gpio"; then
    warning "GPIO group access might not be properly set"
fi

if ! groups pi | grep -q "plugdev"; then
    warning "Camera access might not be properly set"
fi

if ! groups pi | grep -q "dialout"; then
    warning "Serial port access might not be properly set"
fi

# Test database creation
log "Testing database access..."
if sqlite3 "$INSTALL_DIR/timelapse_monitor.db" "CREATE TABLE test (id INTEGER PRIMARY KEY);" 2>/dev/null; then
    sqlite3 "$INSTALL_DIR/timelapse_monitor.db" "DROP TABLE test;"
    log "Database access verified"
else
    warning "Database access might be restricted"
fi

# Verify log directory permissions
log "Verifying log permissions..."
touch "$INSTALL_DIR/logs/test.log" 2>/dev/null && rm "$INSTALL_DIR/logs/test.log"
if [ $? -ne 0 ]; then
    warning "Log directory permissions might be incorrect"
fi

# Test gphoto2 installation
log "Testing gphoto2 installation..."
if ! gphoto2 --version >/dev/null 2>&1; then
    warning "gphoto2 might not be properly installed"
fi

# Test 4G modem
log "Testing 4G modem..."
if [ -e "/dev/ttyUSB0" ]; then
    # Try basic AT command
    if ! echo "AT" > /dev/ttyUSB0; then
        warning "4G modem found but not responding"
    fi
else
    warning "4G modem not detected at /dev/ttyUSB0"
fi

# Enable and start services
log "Enabling and starting services..."
systemctl daemon-reload
systemctl enable wait-for-network.service
systemctl enable timelapse.service
systemctl enable watchdog.service

# Create test notification
log "Testing notification system..."
if [ ! -z "$NTFY_TOPIC" ]; then
    log "Sending test notification to ntfy.sh/$NTFY_TOPIC"
    curl -H "Title: Installation Complete" -H "Priority: 5" -H "Tags: white_check_mark" \
        -d "Timelapse system installation completed successfully" \
        "https://ntfy.sh/$NTFY_TOPIC"
    
    # Verify notification sent
    if [ $? -eq 0 ]; then
        log "Test notification sent successfully"
    else
        warning "Failed to send test notification. Please check your ntfy.sh topic"
    fi
else
    warning "No ntfy.sh topic configured - skipping test notification"
fi

# Print completion message
log "Installation completed!"
log "The timelapse system will automatically start on boot"
log "You can:"
log "- Monitor the service: journalctl -u timelapse -f"
log "- Check status: systemctl status timelapse"
log "- Start manually: sudo systemctl start timelapse"
log "- Stop: sudo systemctl stop timelapse"

# Print any warnings that occurred during installation
if [ -f /tmp/timelapse_install_warnings ]; then
    echo -e "\n${YELLOW}Installation completed with warnings:${NC}"
    cat /tmp/timelapse_install_warnings
    rm /tmp/timelapse_install_warnings
fi