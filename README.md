# VM Disk Dependency Mapper

Finds VirtualBox and VMware disk images (`.vdi` and `.vmdk`) in the folders you
give it, works out which images depend on which, and helps you decide which
ones can be purged.

This is an analysis tool only. It reads disk images and VM configuration files
but never deletes, moves or modifies them. The only files it writes are the
reports and selection files you ask it to save, and it refuses to save them
over any VM file.

Images are organized into groups. A group is a base image plus every snapshot
delta built on it. A group that contains deltas needs consolidating (merging
into a single image) before you extract files from it, because the newest
data is stored in the deltas.

## Requirements

Python 3.8 or later, including tkinter (part of the standard Windows
installer). No extra packages are needed.

## Purge planner (GUI)

```
python vm_disk_purge_gui.py
python vm_disk_purge_gui.py D:\VMs E:\VirtualBox
```

- Add folders and click **Scan**. Folders given on the command line are
  scanned immediately.
- Click the **Purge** box on an image or group to check it. Images that rely
  on a checked file, but are not checked themselves, turn amber because they
  would stop working. Select any row to see what it relies on and what would
  break if it were purged.
- Click the **Extracted** box on a group to record that you have extracted
  its files.
- **Save selections** stores your checkmarks in a JSON file so you can come
  back to them later.
- **Export purge list** writes the checked files, and any warnings, to a text
  file. Deleting the files is up to you.

## Text report

```
python vm_disk_deps.py D:\VMs E:\VirtualBox "F:\Old Backups"
python vm_disk_deps.py D:\VMs -o C:\Temp\disk_report.txt
```

- `-o FILE` sets where the report is saved. The default is
  `vm_disk_dependencies.txt` in the current folder.
- `-v` adds UUIDs, CIDs and parent filename hints to the report.
