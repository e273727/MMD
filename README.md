# e-file MMD · Local document register

A private document register that runs locally or can be shared with other devices on a trusted private network. Accounts separate administrators from upload-only users.

## Windows multi-user setup

Use one Windows computer as the host. Run the packaged `EFileMMD.exe` on that computer; coworkers connect to the LAN address it prints using Chrome, Edge, or another browser. Do not run separate copies on each person's computer if you want one shared library. Keep the host computer awake and the application window open.

### Build the Windows package

On Windows, install Python 3.12 with the Python launcher enabled, then double-click `build-windows.bat`. It installs the packaging dependencies and creates `Windows-EXE\EFileMMD.exe` and `Windows-EXE\EFileMMD-Windows.zip`. Extract the ZIP and run `EFileMMD.exe`. The app's database files and settings are kept in `%LOCALAPPDATA%\EFileMMD` on the host computer.

A Windows build workflow is also included for GitHub Actions. After the workflow file is pushed to the repository, open **Actions → Build Windows app → Run workflow**. Download the `EFileMMD-Windows` artifact from the completed run and extract it on the host computer.

When first launched, Windows Firewall may ask whether Python/EFileMMD can accept incoming connections. Allow it on **Private networks**. Share the **Same-network users** address shown in the console with coworkers on the same trusted Wi-Fi/LAN. They only need that address in their browser; they do not need to install the app.

On first launch, create the administrator account. In Settings, the Admin can create more Admin and Upload user accounts. Upload users can add and view documents; Admins can edit and delete documents, manage settings, and export the Excel register. Use the app only on a trusted private network, and do not forward its port to the public internet.

For development on macOS, run `run.command`; it opens the app on the Mac and prints the same-network URL. Stop either server with Control-C.

## Optional network storage

SQLite and local file storage work without extra packages. For a remote PostgreSQL database, install the optional connector and operating system credential manager integration once from this folder:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements-network.txt
```

`run.command` uses this project-local Python environment automatically when it exists, so the optional PostgreSQL packages are available to the running app without changing macOS's system Python.

Then open **Storage settings** in the app. PostgreSQL needs a reachable server IP/host, port (normally 5432), database, username, password, and permission to create tables and indexes. The app tests the connection before switching. The password is kept in the operating system credential manager; it is not written to the settings file. Local SQLite records are copied to PostgreSQL by document ID when switching. Back up the PostgreSQL database separately.

For SMB files, choose **Open shared folder**, enter the SMB server IP/host and share, then sign in through File Explorer or Finder. Windows Credential Manager or macOS Keychain handles SMB credentials. After the share connects, choose **SMB shared folder** in Storage settings and save. New uploads are written to `e-file MMD/documents` on that share. Existing files remain at their original paths. Back up the SMB share separately.

The PostgreSQL and SMB connections are made by the computer running this app. Every connected user sees the same documents and classification masters. Keep the database and file share reachable from the host computer. The Windows build has its own settings and credential store, so enter PostgreSQL details again on the Windows host. If older attachments were stored only in the Mac app folder, copy them to a shared file location before switching the host to Windows; PostgreSQL stores document records, while the configured file storage holds the actual attachments. The server accepts requests from devices on the LAN without individual accounts, so use it only on a trusted private network and do not forward its port to the public internet. The operating system firewall may ask you to allow incoming connections; allow EFileMMD/Python on your private network.

## Use it

- Use the left navigation to switch between **Home**, **Upload**, and **View Files**. Home shows the library count and storage used. Upload is a full-page form modeled on the supplied MMD project mockup; View Files has a search bar and classification filters; tags are not shown there.
- Open **Settings → Classification masters** to add reusable values for Document Type and Financial Year. Purchase Order, Work Order, and Condemnation are supplied initially. Month is selected from the calendar; previously used items and vendors are suggested on later uploads.
- Add PDF, DOC, DOCX, PNG, JPG/JPEG, GIF, WebP, BMP, or TIFF files up to **250 MB**. The app checks the extension, size, and known file signatures for PDFs and images before saving.
- PDF and image contents preview in the upload dialog. Word files show a pre-save file summary; after saving, use **Open** to view them in the device app associated with Word documents. Word rendering depends on an installed compatible app.
- Enter tags and press Return. Existing tags appear as suggestions. Tags are case-insensitive for reuse. Use multiple tags to assign a combination to a document.
- Tags can be added during upload and reused. The View Files screen does not display or filter by tags.
- Each upload receives a unique MMD serial key and automatic date-time. Classify it by Document Type, Financial Year, Month, Item, and Vendor. Filter by Document Type and Financial Year, or search by filename, serial key, or classification text.
- Use **Preview** for an inline PDF/image preview, or **Open** to hand the attachment to your browser or associated app.
- The dashboard reports total document storage in MB or GB. Admins can export an Excel file containing the unique key, document name, classifications, and upload date-time. **Activity logs** shows timestamped failures and lets you copy them. Client-side failures are kept in this browser; server-side upload failures are saved in the local database.
- If the page says it cannot reach the local app, check that the Terminal window running it is still open. Restart `run.command` and use the address it opens.
- If Storage settings reports that the server is out of date, stop the older app with Control-C in its Terminal window, run `run.command` again, and open the exact address printed there. When port 8765 is already occupied, the updated app selects and opens another local port.

## Local data and backup

With a source-code run, local documents live in `data/documents/`; the SQLite index and tags live in `data/efile.sqlite3`. The packaged Windows host stores app data in `%LOCALAPPDATA%\EFileMMD`. Back up the whole `data/` folder while the app is stopped. When PostgreSQL or SMB is selected, back up those services separately as well. Do not edit app data files by hand. Removing the app folder removes the local library, so keep separate backups if the records matter.

The app is intended to run on one trusted host computer for multiple users on its private LAN. It does not encrypt files at rest; protect the host with an operating-system account and disk encryption, and back up the PostgreSQL database and SMB share separately. The browser connections use plain HTTP on the local network, so do not expose the app to the public internet.
