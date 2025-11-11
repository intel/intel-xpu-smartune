from distutils.core import setup
from setuptools import Extension
from Cython.Build import cythonize
import os
import re

# python setup.py build

def find_py_files(base_path, exclude_dirs=None, exclude_files=None):
    """递归查找所有.py文件，可自定义排除目录和文件路径

    Args:
        base_path: 搜索的根目录
        exclude_dirs: 要排除的目录名集合（如 {'config', 'test'}）
        exclude_files: 要排除的完整文件路径集合（如 {'web/webui.py'}）
    """
    exclude_dirs = exclude_dirs or {'config', 'test'}  # 默认排除目录
    exclude_files = exclude_files or set()  # 默认排除文件
    py_files = []

    for root, _, files in os.walk(base_path):
        # 跳过隐藏目录和排除目录
        if (os.path.basename(root).startswith('.') or
                os.path.basename(root) in exclude_dirs):
            continue

        for file in files:
            if file.endswith('.py'):
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, base_path)

                # 检查是否在排除列表中
                if rel_path.replace(os.path.sep, '/') not in exclude_files:
                    py_files.append(full_path)

    return py_files


def path_to_module_name(filepath, base_path):
    """将文件路径转换为模块名称"""
    rel_path = os.path.relpath(filepath, base_path)
    module_path = os.path.splitext(rel_path)[0]
    return module_path.replace(os.path.sep, '.')


def get_so_path(py_path):
    """获取编译后的.so文件路径"""
    dirname = os.path.dirname(py_path)
    basename = os.path.basename(py_path)
    so_pattern = re.compile(r'^{}(\.cpython-\d+[a-z]*-[a-z0-9_-]*)?\.so$'.format(
        re.escape(os.path.splitext(basename)[0])
    ))

    for f in os.listdir(dirname):
        if so_pattern.match(f):
            return os.path.join(dirname, f)
    return None


def clean_py_files(extensions):
    """编译完成后删除.py文件"""
    for ext in extensions:
        py_file = ext.sources[0]
        if os.path.exists(py_file):
            print(f"Removing {py_file}")
            os.remove(py_file)


current_dir = os.path.dirname(os.path.abspath(__file__))
all_py_files = [
    f for f in find_py_files(
        current_dir,
        exclude_dirs={'config', '__pycache__'},
        exclude_files={'web/webui.py', 'BalanceService.py'}
    )
    if not os.path.basename(f) == 'setup.py'
]

print(f"all_py_files: {all_py_files}")

extensions = [
    Extension(
        path_to_module_name(f, current_dir),
        [f],
        extra_compile_args=["-O3"]
    )
    for f in all_py_files
]

# 构建完成后执行清理
setup(
    ext_modules=cythonize(
        extensions,
        language_level="3",
        compiler_directives={
            'always_allow_keywords': True,
            'language_level': '3'
        },
        build_dir="build"
    ),
    script_args=['build_ext', '--inplace']
)

# 修改后的安全判断
compiled_files = [get_so_path(f) for f in all_py_files]
if all(compiled_files):
    clean_py_files(extensions)
else:
    missing = [f for f, so in zip(all_py_files, compiled_files) if not so]
    print("Warning: Missing .so files for:")
    for f in missing:
        print(f" - {f}")
    print("Skipping .py deletion")
