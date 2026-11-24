from PyInstaller.utils.hooks import collect_data_files

# Search-mode tokenization uses the dictionary and the HMM tables in Python modules.
datas = collect_data_files("jieba", includes=["dict.txt"])
