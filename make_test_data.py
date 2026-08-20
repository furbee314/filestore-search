"""Generate a synthetic file store that mimics the real layout:

    <out>/Software_Library/Admin_Software/...
    <out>/Software_Library/Drivers/...
    <out>/Software_Library/User_Software/...
    <out>/repos/RHEL8/...        (vendor repo scheme: packages/<arch>/<name>/...)
    <out>/repos/RHEL9/...
    <out>/repos/Ubuntu/...       (vendor pool scheme: pool/<comp>/<letter>/<pkg>/...)
    <out>/isos/...

Used for end-to-end testing of the index + search pipeline.
"""
import os
import sys


FILES = [
    # --- vendor repo scheme: RHEL (packages/<arch>/<name>/name-version-release.arch.rpm) ---
    "repos/RHEL8/packages/x86_64/kernel/kernel-4.18.0-513.el8.x86_64.rpm",
    "repos/RHEL8/packages/x86_64/kernel/kernel-4.18.0-513.el8.aarch64.rpm",
    "repos/RHEL8/packages/noarch/dell-os-pro-support/dell-os-pro-support-8.9-1.el8.noarch.rpm",
    "repos/RHEL9/packages/x86_64/kernel/kernel-5.14.0-362.8.1.el9_4.x86_64.rpm",
    "repos/RHEL9/packages/x86_64/dell-om-agent/dell-om-agent-7.4.0-1.el9.x86_64.rpm",
    "repos/RHEL9/packages/x86_64/hp-ilo-5-agent/hp-ilo-5-agent-1.5.2-2.el9.x86_64.rpm",
    "repos/RHEL9/packages/x86_64/nvidia-driver/nvidia-driver-535.183.01-1.el9.x86_64.rpm",
    "repos/RHEL9/packages/noarch/intel-ucode/intel-ucode-2.20230912-1.el9.noarch.rpm",
    "repos/RHEL9/packages/noarch/microcode_ctl/microcode_ctl-2.1-42.el9.noarch.rpm",
    "repos/RHEL9/packages/src/kernel/kernel-5.14.0-362.8.1.el9_4.src.rpm",
    # --- vendor pool scheme: Ubuntu (pool/<component>/<letter>/<pkg>/<pkg>_<ver>_<arch>.deb) ---
    "repos/Ubuntu/pool/main/l/linux-firmware-nonfree/linux-firmware-nonfree_1.20230822-0ubuntu3_amd64.deb",
    "repos/Ubuntu/pool/main/h/hwe-addon/hwe-addon_22.04.11_amd64.deb",
    "repos/Ubuntu/pool/main/n/nvidia-graphics-drivers-535/nvidia-graphics-drivers-535_535.183.01-0ubuntu3.22.04.1_amd64.deb",
    "repos/Ubuntu/pool/universe/d/dell-om-agent/dell-om-agent_7.4.0-1ubuntu24.04_amd64.deb",
    "repos/Ubuntu/pool/universe/r/realtek-rtl8852be-dkms/realtek-rtl8852be-dkms_1.0-2_all.deb",
    "repos/Ubuntu/pool/universe/s/supermicro-ipmi-sol/supermicro-ipmi-sol_1.39.25-1_amd64.deb",
    # --- Software_Library / Admin_Software (firmware, BIOS, management tools) ---
    "Software_Library/Admin_Software/dell/dell-bios-r740-x4.4.4-a01.zip",
    "Software_Library/Admin_Software/dell/dell-idrac8-firmware-5.10.90.90-a02.zip",
    "Software_Library/Admin_Software/dell/dell-raid-9560-1.12.4.0015-firmware.zip",
    "Software_Library/Admin_Software/hp/hp-proliant-dl380-gen10-bios-s01-011.zip",
    "Software_Library/Admin_Software/hp/hp-hpe-ilo-5-2.85-firmware.zip",
    "Software_Library/Admin_Software/lenovo/lenovo-thinkpad-t14-firmware-nic-2.24.0.zip",
    "Software_Library/Admin_Software/lenovo/lenovo-bios-t14-gen3-mncn25ww.zip",
    "Software_Library/Admin_Software/supermicro/x11spi-bios-5.23-a2.zip",
    "Software_Library/Admin_Software/supermicro/supermicro-ipmi-sol-firmware-1.39.25.zip",
    "Software_Library/Admin_Software/nvme/seagate-nvme-firmware-v4.0.0.zip",
    "Software_Library/Admin_Software/nvme/samsung-980-pro-nvme-firmware-ext4a6ls.zip",
    "Software_Library/Admin_Software/cpu/intel-microcode-data-20230912.zip",
    "Software_Library/Admin_Software/cpu/amd-cpu-firmware-2023-Q4.zip",
    # --- Software_Library / Drivers ---
    "Software_Library/Drivers/windows/dell/dell-om-agent-7.4.0-win-x64.msi",
    "Software_Library/Drivers/windows/dell/dell-utility-updater-3.11.0.0-win-exe.exe",
    "Software_Library/Drivers/windows/dell/Dell_BIOS_R740_X4.4.4_A01.exe",
    "Software_Library/Drivers/windows/hp/hp-smart-array-driver-2.2.16-win64.msi",
    "Software_Library/Drivers/windows/hp/HP_BIOS_ProLiant_Gen10_S01_011.exe",
    "Software_Library/Drivers/windows/lenovo/lenovo-thinkpad-t14-firmware-3.2.0-win-x64.msi",
    "Software_Library/Drivers/windows/lenovo/Lenovo_BIOS_T14_Gen3_MNCN25WW.exe",
    "Software_Library/Drivers/windows/intel/intel-rst-v21.7.4.1041-win-x64.exe",
    "Software_Library/Drivers/windows/realtek/rtk6287-audio-driver-6.0.9298.1-win10-win11.exe",
    "Software_Library/Drivers/windows/nvidia/nvidia-desktop-app-552.22-win10-win11-64-bit-desktop-intel.exe",
    "Software_Library/Drivers/windows/supermicro/supermicro-ipmi-remote-access-1.39.25-win-x64.msi",
    "Software_Library/Drivers/linux/source/linux-5.15.150.tar.xz",
    "Software_Library/Drivers/linux/source/linux-firmware-20231205.tar.gz",
    # --- Software_Library / User_Software (apps for workstations/servers) ---
    "Software_Library/User_Software/windows/visual-cpp-redistributable-2015-2022-x64.exe",
    "Software_Library/User_Software/windows/powershell-7.4.3-win-x64.msi",
    "Software_Library/User_Software/misc/readme-how-to-install-drivers.txt",
    # --- installers without a conventional extension / via .sh (priority 3) ---
    "Software_Library/Drivers/linux/symantec/SymantecLinuxInstaller",
    "Software_Library/Drivers/linux/symantec/install-symantec-endpoint.sh",
    # --- Windows Update / patch bundles (.msu, priority 3) ---
    "Software_Library/Drivers/windows/dell/dell-om-agent-7.4.0-win-x64.msu",
    # --- repo metadata / manifests (priority 1, demoted below deliverables) ---
    "repos/RHEL9/repodata/repomd.xml",
    "repos/RHEL9/Packages.gz",
    # --- isos (install / boot media) ---
    "isos/rhel-9.4-x86_64-dvd.iso",
    "isos/rhel-9.4-aarch64-dvd.iso",
    "isos/ubuntu-24.04-live-server-amd64.iso",
    "isos/windows-server-2022-standard-x64.iso",
    "isos/esxi-8.0-update-1.iso",
    "isos/windows11-24h2-eu-x64.iso",
    # --- checksum sidecars (must be excluded from the index) ---
    "Software_Library/Admin_Software/dell/dell-bios-r740-x4.4.4-a01.zip.sha256",
    "repos/RHEL9/packages/x86_64/nvidia-driver/nvidia-driver-535.183.01-1.el9.x86_64.rpm.sha128",
    "isos/rhel-9.4-x86_64-dvd.iso.sha256",
    "Software_Library/Drivers/linux/source/linux-firmware-20231205.tar.gz.sha1",
    "Software_Library/Admin_Software/lenovo/lenovo-bios-t14-gen3-mncn25ww.zip.md5",
]


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "data"
    for rel in FILES:
        full = os.path.join(out, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        if not os.path.exists(full):
            with open(full, "wb") as f:
                # vary sizes a bit so size display isn't all zero
                f.write(b"x" * (len(rel) * 97 % 500000 + 1024))
    print(f"wrote {len(FILES)} synthetic files under {out!s}")


if __name__ == "__main__":
    main()
