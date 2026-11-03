# Setting up the mini PC

One time only, about 5 minutes. Windows stays exactly as it is.

## 1. Make the PC start from the stick

1. Plug the stick into the mini PC. Use a blue USB port if it has one (that's USB 3, faster).
2. In Windows, open **Start**, then **Settings**, then **System**, then **Recovery**.
3. Next to **Advanced startup**, click **Restart now**, then **Restart now** again.
   You should see a blue screen titled "Choose an option".
4. Click **Troubleshoot**, then **Advanced options**, then **UEFI Firmware Settings**, then **Restart**.
   The PC opens its BIOS: a grey or blue screen full of settings.
   If there's no "UEFI Firmware Settings" button: shut down, then press the power button and
   tap **Del** (or **F2**) over and over until the BIOS appears.
5. Use the arrow keys to open the **Boot** tab.
6. If there's a **Fast Boot** setting, set it to **Disabled** (it can skip USB sticks).
7. Select **Boot Option #1** and press **Enter**. Pick the USB stick: its entry may show the
   brand (**SanDisk**) or just something like **USB Partition 1**. If there are two, pick the
   one that starts with **UEFI**.
8. Press **F4** or **F10** to save and exit, and confirm with **Yes**.

## 2. First start

The PC restarts and shows its logo with a spinner. The first start takes about a minute
because the stick sets itself up. Then the home screen appears: the time, Netflix, YouTube.

If Windows starts instead, with no message, do steps 1.2 to 1.8 again. If Windows still
starts, or you see **"Secure Boot Violation"**:

1. First, in Windows, search for **Device encryption** in Settings. If it's **On**, open
   aka.ms/myrecoverykey on your phone and keep the recovery key at hand. Windows asks for it
   once after the next step.
2. Go into the BIOS again (step 1.2 to 1.4), open the **Security** tab, set **Secure Boot** to
   **Disabled**, then save with **F4** or **F10**.

## 3. On the TV

- No network cable? A Wi-Fi panel opens. Pick your network and type the password with the
  keyboard on the back of the remote.
- Click **Netflix** and sign in once. It remembers you.
- **Home** on the remote brings you back to the home screen. If it doesn't, point at the very
  top of the screen and click the house icon.
- **Add Website** (the plus tile) pins any other site. **Settings** (the gear icon at the top
  right) has Wi-Fi, Bluetooth, the VPN, sound output, text size and editing the home screen.
- Put your Bluetooth headphones in pairing mode, open **Settings**, then **Bluetooth**, and
  pick them under **Other Devices**. The sound moves to them. To move it back, open
  **Settings**, then **Sound Output**.
- The VPN needs a NordVPN account. Open **Settings**, then **VPN**, then **Sign In**, and type
  your email and password on Nord's website. When the TV brings you back, switch **VPN** on.
  To have it on every time the TV starts, also turn on **Connect at Startup**.
- To turn the TV off, use the remote's power button, or the power icon at the top right, then
  **Turn Off**.

## Back to Windows

- Once: the power icon, then **Restart in Windows**. Restarting from Windows comes back here.
- For good: pull the stick out and restart. Windows starts as before.
