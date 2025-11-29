import yaml
from jinja2 import Environment, FileSystemLoader
import os

def build_site():
    # 1. 设置文件路径
    data_file = 'data.yaml'
    template_file = 'template.html'
    output_file = 'index.html'

    print(f"Loading data from {data_file}...")
    
    # 2. 读取 YAML 数据
    try:
        with open(data_file, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        print(f"Error: {data_file} not found.")
        return
    except yaml.YAMLError as exc:
        print(f"Error parsing YAML: {exc}")
        return

    # 3. 设置 Jinja2 环境
    file_loader = FileSystemLoader('.')
    env = Environment(loader=file_loader)
    
    try:
        template = env.get_template(template_file)
    except Exception as e:
        print(f"Error loading template: {e}")
        return

    print("Rendering HTML...")
    
    # 4. 渲染模板
    try:
        output_html = template.render(data)
    except Exception as e:
        print(f"Error rendering template: {e}")
        return

    # 5. 写入最终的 HTML 文件
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(output_html)

    print(f"Success! Website generated at: {os.path.abspath(output_file)}")

if __name__ == "__main__":
    build_site()