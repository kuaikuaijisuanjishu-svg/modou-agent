"""Build a local VSIX without installing packaging dependencies."""
import json
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape
root = Path(__file__).resolve().parent
manifest = json.loads((root / 'package.json').read_text())
output = Path(sys.argv[1])
with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
    archive.writestr('[Content_Types].xml', '''<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="json" ContentType="application/json"/><Default Extension="js" ContentType="application/javascript"/><Default Extension="md" ContentType="text/markdown"/><Default Extension="vsixmanifest" ContentType="text/xml"/></Types>''')
    archive.writestr('extension.vsixmanifest', f'''<?xml version="1.0" encoding="utf-8"?><PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011"><Metadata><Identity Language="en-US" Id="{escape(manifest['name'])}" Version="{manifest['version']}" Publisher="{manifest['publisher']}"/><DisplayName>{escape(manifest['displayName'])}</DisplayName><Description xml:space="preserve">{escape(manifest['description'])}</Description><Tags>testing</Tags><Categories>Testing</Categories><GalleryFlags>Public</GalleryFlags><Properties><Property Id="Microsoft.VisualStudio.Code.Engine" Value="{manifest['engines']['vscode']}"/><Property Id="Microsoft.VisualStudio.Code.ExtensionDependencies" Value=""/><Property Id="Microsoft.VisualStudio.Code.ExtensionPack" Value=""/></Properties></Metadata><Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation><Dependencies/><Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/><Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" Addressable="true"/></Assets></PackageManifest>''')
    for name in ('package.json', 'extension.js', 'README.md'):
        archive.write(root / name, 'extension/' + name)
print(output)
