# 生成（JP）
- name: Generate story markdown (JP)
  env:
    PYTHONUNBUFFERED: "1"
  run: |
    python -u automation/generate_story.py --config automation/config.yml

# 投稿（JP） 既存の note_draft.py を活用
- name: Post draft to Note (JP)
  env:
    PYTHONUNBUFFERED: "1"
  run: |
    python -u automation/note_draft.py --lang jp --headless "${{ github.event.inputs.headless || 'true' }}"
