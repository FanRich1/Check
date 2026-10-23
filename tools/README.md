# OCR 临时链接工具

此目录不随项目分发可执行程序。需要临时 OCR 链接时，从 [Cloudflare 官方下载页](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/) 获取适合本机的 cloudflared。

Windows 将文件命名为 `cloudflared.exe` 放在本目录。也可安装时显式指定已下载文件：

```powershell
py -3.12 scripts/install_windows.py --local-ocr --cloudflared "C:\下载目录\cloudflared-windows-amd64.exe"
```

网页启动时先检查这里，再兼容开发机旧路径 `outputs/stage2/tunnel/cloudflared.exe`。不需要指定 OCR 的离线示例不依赖此工具；已有直接下载 URL 映射也可替代临时链接。具体操作见 [交付说明](../docs/delivery-guide.md)。
