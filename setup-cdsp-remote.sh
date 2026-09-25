# Function to install Bluetooth Remote Script and LED Display module
install_bluetooth_remote() {
    echo "🎮 Installing Bluetooth Remote Control & LED module..."
    sudo apt install -y python3-evdev python3-websocket python3-pip

    echo "📥 Downloading remote.py and tm1637_lgpio from GitHub..."
    wget -q https://raw.githubusercontent.com/melomane63/Remote-and-Display-Camilladsp/main/remote.py -O ~/remote.py
    
    # Télécharger le module tm1637_lgpio si ce n'est pas un paquet pip classique
    wget -q https://raw.githubusercontent.com/melomane63/tm1637_lgpio/main/tm1637_lgpio.py -O ~/tm1637_lgpio.py

    # S'assurer que les dépendances lgpio sont présentes
    sudo apt install -y python3-lgpio

    cat > ~/remote.service <<EOL
[Unit]
Description=Bluetooth Remote Control for CamillaDSP
After=bluetooth.target network.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=$USER
WorkingDirectory=/home/$USER
ExecStart=/usr/bin/python3 /home/$USER/remote.py
Restart=on-failure
RestartSec=5
KillMode=mixed
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOL

    sudo mv ~/remote.service /lib/systemd/system/remote.service
    sudo systemctl daemon-reload
    sudo systemctl enable remote.service
    sudo systemctl start remote.service
    echo "✅ Remote & LED module installed and started!"
}
