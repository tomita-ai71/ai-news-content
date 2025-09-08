cd ~/note-automation/content
echo "<!-- refresh login -->" >> jp/input.md
git add jp/input.md
git commit -m "refresh login"
git push origin main
