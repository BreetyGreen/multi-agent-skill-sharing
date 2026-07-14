using System.Runtime.InteropServices;

namespace Myco;

/// 真·背景模糊，对齐 macOS 版 NSPopover 的毛玻璃材质。
/// 首选 accent acrylic（SetWindowCompositionAttribute）：色调/浓度完全自控，
/// 玻璃感明显；系统 backdrop（DWMSBT）自带的深色染层太重，会把背景压没。
/// 圆角仍由 DWM 提供。失败返回 false，调用方回退为不透明背景。
public static class GlassHelper
{
    private const int DWMWA_USE_IMMERSIVE_DARK_MODE = 20;
    private const int DWMWA_WINDOW_CORNER_PREFERENCE = 33;  // DWMWCP_ROUND = 2
    private const int WCA_ACCENT_POLICY = 19;
    private const int ACCENT_ENABLE_ACRYLICBLURBEHIND = 4;

    [StructLayout(LayoutKind.Sequential)]
    private struct AccentPolicy
    {
        public int AccentState;
        public int AccentFlags;
        public uint GradientColor;   // ABGR：0xAA BB GG RR
        public int AnimationId;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct WindowCompositionAttribData
    {
        public int Attribute;
        public IntPtr Data;
        public int SizeOfData;
    }

    [DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int value, int size);

    [DllImport("user32.dll")]
    private static extern int SetWindowCompositionAttribute(IntPtr hwnd, ref WindowCompositionAttribData data);

    public static bool Apply(IntPtr hwnd, bool dark)
    {
        var darkMode = dark ? 1 : 0;
        DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ref darkMode, 4);

        var round = 2;
        DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ref round, 4);

        // 玻璃基调（ABGR）：深色 #14170C @ ~40%，浅色 #F2F1E9 @ ~55%。
        // 剩余的品牌色渐变由 Theme 的半透明染层补足。
        var accent = new AccentPolicy
        {
            AccentState = ACCENT_ENABLE_ACRYLICBLURBEHIND,
            AccentFlags = 2,
            GradientColor = dark ? 0x660C1714u : 0x8CE9F1F2u,
        };
        var size = Marshal.SizeOf<AccentPolicy>();
        var ptr = Marshal.AllocHGlobal(size);
        try
        {
            Marshal.StructureToPtr(accent, ptr, false);
            var data = new WindowCompositionAttribData
            {
                Attribute = WCA_ACCENT_POLICY,
                Data = ptr,
                SizeOfData = size,
            };
            return SetWindowCompositionAttribute(hwnd, ref data) != 0;
        }
        finally
        {
            Marshal.FreeHGlobal(ptr);
        }
    }
}
