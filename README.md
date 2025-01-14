# EPB TL01

A robust monitoring system for professional timelapse capture, designed for maximum reliability and immediate error notification. The system automatically starts on boot and includes comprehensive error handling and notifications.

## Features

- Automated image monitoring and upload to OneDrive
- Real-time notifications via ntfy.sh with priority levels
- Power failure detection and notification
- Persistent file tracking with SQLite
- Automatic backup system for failed uploads
- System health monitoring (temperature, disk space)
- Exponential backoff for error recovery
- 4G connectivity with SMS fallback for notifications
- Automatic startup on boot with network waiting
- Hardware watchdog for system recovery

## Hardware Requirements

- Raspberry Pi Zero 2 W
- Canon Camera (Compatible models: R5, R3, 5DMK3, etc.)
- Waveshare SIM7600G-H 4G HAT (B)
- Eaton 3S Mini UPS
- USB cables and power supplies
- Weather-resistant enclosure (recommended)

## Quick Start

1. Clone the repository:
```bash
git clone https://github.com/your-org/timelapse-monitor.git
cd timelapse-monitor
```

2. Make the installation script executable:
```bash
chmod +x install.sh
```

2. Run the installation:
```bash
sudo ./install.sh
```

3. Follow the interactive prompts to configure:
   - OneDrive credentials
   - ntfy.sh topic
   - System settings (intervals, thresholds)

The system will automatically start on boot after installation.

## Configuration

During installation, you'll be prompted for:

### OneDrive Settings
- Client ID
- Client Secret

### Notification Settings
- ntfy.sh topic name

### System Settings (with defaults)
- Check interval (60 seconds)
- Disk warning threshold (25%)
- Disk critical threshold (10%)
- Temperature warning threshold (70°C)
- Temperature critical threshold (80°C)
- Maximum retry attempts (5)

All settings are stored in `/opt/timelapse/config/.env` and can be modified later.

## Service Management

The timelapse system runs as a systemd service and starts automatically on boot.

### Basic Commands
```bash
# Check service status
sudo systemctl status timelapse

# View logs
journalctl -u timelapse -f

# Manual control (if needed)
sudo systemctl stop timelapse
sudo systemctl start timelapse
sudo systemctl restart timelapse
```

### Boot Process
1. System starts
2. Waits for network connectivity
3. Initializes hardware watchdog
4. Starts timelapse service

## Notification Priority Levels

1. CRITICAL (5): System down, critical failures
2. HIGH (4): Power issues, storage critical
3. MEDIUM (3): Upload failures, camera issues
4. LOW (2): Recovery events, disk warnings
5. INFO (1): Self-healing events

## Directory Structure

```
/opt/timelapse/
├── config/
│   └── .env         # Configuration file
├── backup/          # Local backup storage
├── logs/           # Application logs
└── timelapse_monitor.py
```

## Maintenance

### Regular Checks

1. Monitor disk space usage
2. Check backup directory size
3. Verify database integrity
4. Test notification system
5. Check system temperature
6. Verify OneDrive connectivity

### Database Maintenance

```bash
# Backup database
sqlite3 timelapse_monitor.db ".backup 'backup.db'"

# Check database integrity
sqlite3 timelapse_monitor.db "PRAGMA integrity_check;"
```

### Log Rotation

Logs are automatically rotated daily and kept for 7 days.

## Troubleshooting

### Common Issues

1. Camera Connection Lost
   - Check USB connections
   - Verify camera power
   - Check for camera sleep settings

2. Upload Failures
   - Verify 4G connection
   - Check OneDrive token expiration
   - Verify available storage

3. System Not Responding
   - Check system temperature
   - Verify power supply
   - Check available disk space

4. Boot Issues
   - Check network connectivity
   - Verify service status
   - Check hardware watchdog status

### Log Locations

- Application log: `/opt/timelapse/logs/timelapse_monitor.log`
- System service log: `journalctl -u timelapse`
- Installation log: `/var/log/timelapse_install.log`
- Database: `timelapse_monitor.db`

### Recovery Commands

```bash
# Reset USB devices
sudo usbreset /dev/bus/usb/XXX/YYY

# Force service restart
sudo systemctl reset-failed timelapse
sudo systemctl restart timelapse

# Check network wait status
systemctl status wait-for-network
```

## Security Considerations

1. Physical Security
   - Secure the Raspberry Pi and camera
   - Protect USB connections
   - Lock enclosure

2. Network Security
   - Use secure topics for ntfy.sh
   - Regularly update OneDrive tokens
   - Monitor for unauthorized access

3. Boot Security
   - Hardware watchdog enabled
   - Network wait configured
   - Service dependencies managed

## Support

For support, please create an issue in the repository or contact the maintenance team.

## Contributing

Please read [CONTRIBUTING.md](CONTRIBUTING.md) for details on our code of conduct and the process for submitting pull requests.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- gphoto2 project for camera control
- ntfy.sh for notification system
- OneDrive SDK team