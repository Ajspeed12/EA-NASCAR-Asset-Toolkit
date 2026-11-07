# EA NASCAR Asset Toolkit v1.0 (PS2/Xbox)

1. Place matching CARDATA.DAT and NASCAR.DB in source.
2. Run "Extract Assets.bat" from this root folder.
3. Run "Convert Assets.bat" to convert models and textures.

### Output Folders

```text
CARDATA\           Original extracted assets
converted_models\  OBJ models
decoded_textures\  PNG textures
```

Requires Windows, QuickBMS (included), Python 3.9+ with numpy and Pillow.
Install dependencies: python -m pip install numpy Pillow

### Third-Party Software

This toolkit includes **QuickBMS**, developed by **Luigi Auriemma (aluigi)**, for extracting game archives.

QuickBMS is distributed under the **GNU General Public License version 2 (GPL-2.0)**. Its licence is included in `tools/LICENSE-GPL-2.0.txt`.

- **QuickBMS website:** https://aluigi.altervista.org/quickbms.htm

- **Original author:** Luigi Auriemma

The NASCAR 06 Asset Toolkit's original scripts are distributed under the MIT License. QuickBMS remains subject to its own licence and is not covered by the toolkit's MIT License.

**Acknowledgement:** Special thanks to Luigi Auriemma for developing QuickBMS and making game archive extraction accessible to the modding and reverse-engineering communities.

### Development Acknowledgement

This toolkit was developed with extensive assistance from OpenAI's ChatGPT, including reverse engineering, code development, debugging, testing and documentation.

The resulting tools have been tested and refined against NASCAR 06 game assets for PlayStation 2 and Xbox.
