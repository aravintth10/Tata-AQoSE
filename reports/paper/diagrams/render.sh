#!/usr/bin/env bash
# Renders every *.mmd here to a cropped vector PDF for the report:
#   reports/paper/diagrams/render.sh
# Needs google-chrome, network access to the Mermaid CDN, and pdfcrop.
set -euo pipefail
cd "$(dirname "$0")"
tmp=$(mktemp -d)
trap 'rm -rf -- "${tmp:?}"' EXIT
for src in *.mmd; do
    name=${src%.mmd}
    {
        echo '<!doctype html><html><head><meta charset="utf-8"><style>'
        echo '@page { size: 1500px 4200px; margin: 0 } body { margin: 20px; background: #fff; font-family: Arial, sans-serif }'
        echo '</style></head><body><pre class="mermaid">'
        sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g; s/&lt;br\/&gt;/<br\/>/g' "$src"
        echo '</pre><script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"></script>'
        echo '<script>mermaid.initialize({startOnLoad:true,theme:"base",flowchart:{curve:"basis",htmlLabels:true},'
        echo 'themeVariables:{fontFamily:"Arial, sans-serif",fontSize:"18px",primaryColor:"#ffffff",primaryBorderColor:"#5242c4",'
        echo 'primaryTextColor:"#171a2b",lineColor:"#171a2b",clusterBkg:"#f6f7fa",clusterBorder:"#dcdfe8",edgeLabelBackground:"#ffffff"}});</script>'
        echo '</body></html>'
    } > "$tmp/$name.html"
    google-chrome --headless=new --disable-gpu --no-pdf-header-footer --virtual-time-budget=10000 \
        --print-to-pdf="$tmp/$name.pdf" "file://$tmp/$name.html" 2>/dev/null
    pdfcrop --margins 4 "$tmp/$name.pdf" "$name.pdf" >/dev/null
    echo "$name.pdf"
done
