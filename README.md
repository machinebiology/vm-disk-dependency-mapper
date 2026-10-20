# VM Disk Dependency Mapper

Finds VirtualBox and VMware disk images (`.vdi` and `.vmdk`) in the folders you
give it. For each image, it lists the image files that it relies on (its
parent, grandparent, and so on) and the image files that rely on it. The
results are written to a text file.

## Requirements

Python 3.8 or later. No extra packages are needed.

## Usage

```
python vm_disk_deps.py D:\VMs E:\VirtualBox "F:\Old Backups"
python vm_disk_deps.py D:\VMs -o C:\Temp\disk_report.txt
```

- `-o FILE` sets where the report is saved. The default is
  `vm_disk_dependencies.txt` in the current folder.
- `-v` adds UUIDs, CIDs and parent filename hints to the report.
