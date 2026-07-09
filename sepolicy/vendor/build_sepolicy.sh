#!/bin/bash
#  Clean up and move private system policies to the correct partition tree
VENDOR_DIR="device/oneplus/sm8250-common/sepolicy/vendor"
PRIVATE_DIR="device/oneplus/sm8250-common/sepolicy/private"

# Ensure we are running from the Android build root
if [ ! -d "device/oneplus/sm8250-common" ]; then
    echo "❌ Error: Run this script from the root of your Android build directory!"
    exit 1
fi

echo "🧹 Cleaning up misplaced vendor policies..."

# 1. Safely remove kcmdlinectrl and misctrl from vendor
if [ -f "$VENDOR_DIR/kcmdlinectrl.te" ]; then
    rm "$VENDOR_DIR/kcmdlinectrl.te"
    echo "  - Removed $VENDOR_DIR/kcmdlinectrl.te"
fi

if [ -f "$VENDOR_DIR/misctrl.te" ]; then
    rm "$VENDOR_DIR/misctrl.te"
    echo "  - Removed $VENDOR_DIR/misctrl.te"
fi

# 2. Ensure private directory exists
echo "📁 Preparing private policy directory..."
mkdir -p "$PRIVATE_DIR"

# 3. Create the policies under private/
echo "📝 Creating kcmdlinectrl.te in private/..."
cat << 'EOF' > "$PRIVATE_DIR/kcmdlinectrl.te"
# Allow command line controller daemon to read device tree configurations
r_dir_file(kcmdlinectrl, sysfs_dt_firmware_android)
EOF

echo "📝 Creating misctrl.te in private/..."
cat << 'EOF' > "$PRIVATE_DIR/misctrl.te"
# Allow device state manager to read device tree config parameters
r_dir_file(misctrl, sysfs_dt_firmware_android)
EOF

echo "✅ SEPolicy successfully restructured!"
echo "👉 You can now re-run your build command."