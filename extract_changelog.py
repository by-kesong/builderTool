import re
import sys

def main():
    tag = sys.argv[1] if len(sys.argv) > 1 else "v1.0.2"
    
    # 去除 tag 的 'refs/tags/' 前缀
    tag = tag.split('/')[-1]
    
    try:
        with open("README.md", "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"Failed to read README.md: {e}")
        sys.exit(1)
        
    # 匹配指定版本标题开始的块，直到下一个 "##" 或者文件末尾
    pattern = rf"(##\s+{re.escape(tag)}.*?\n)(.*?)(?=\n##\s+|\Z)"
    match = re.search(pattern, content, re.DOTALL)
    
    if match:
        changelog = match.group(2).strip()
        with open("RELEASE_NOTES.md", "w", encoding="utf-8") as f:
            f.write(changelog)
        print(f"Successfully extracted changelog for {tag} to RELEASE_NOTES.md")
    else:
        print(f"Could not find changelog section for {tag} in README.md")
        with open("RELEASE_NOTES.md", "w", encoding="utf-8") as f:
            f.write(f"Release version {tag}")

if __name__ == "__main__":
    main()
