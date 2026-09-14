"""WPS 单元格图片：只打包完整原文件，显示缩放由 DISPIMG 完成。"""
from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image


CELL_IMAGE_REL = "http://www.wps.cn/officeDocument/2020/cellImage"
CELL_IMAGE_CONTENT_TYPE = "application/vnd.wps-officedocument.cellimage+xml"
_IMAGE_TYPES = {
    "PNG": ("png", "image/png"),
    "JPEG": ("jpeg", "image/jpeg"),
    "WEBP": ("webp", "image/webp"),
}


class OriginalImageError(ValueError):
    """单张原图不可用时在其单元格显示原因，不影响其他图片和评分导出。"""


@dataclass(frozen=True)
class CellImage:
    name: str
    index: int
    extension: str
    content_type: str
    width: int
    height: int

    @property
    def filename(self) -> str:
        return f"original_image{self.index}.{self.extension}"

    @property
    def formula(self) -> str:
        return f'DISPIMG("{self.name}",1)'


class WpsCellImages:
    """逐张写入 ZIP，内存只保留图片索引；相同文件字节只嵌入一次。"""

    def __init__(self, archive: zipfile.ZipFile):
        self.archive = archive
        self.images: dict[str, CellImage] = {}

    def add(self, path: str, expected_sha256: str | None = None) -> CellImage:
        if not path:
            raise OriginalImageError("未提供原图路径")
        try:
            raw = Path(path).read_bytes()
        except FileNotFoundError as exc:
            raise OriginalImageError("原图文件缺失") from exc
        except OSError as exc:
            raise OriginalImageError("原图文件无法读取") from exc

        digest = hashlib.sha256(raw).hexdigest()
        if expected_sha256 and digest != expected_sha256:
            raise OriginalImageError("原图已变更，未嵌入")
        if digest in self.images:
            return self.images[digest]
        try:
            with Image.open(BytesIO(raw)) as source:
                if source.format not in _IMAGE_TYPES or getattr(source, "n_frames", 1) != 1:
                    raise OriginalImageError("原图不是静态 PNG/JPEG/WebP")
                extension, content_type = _IMAGE_TYPES[source.format]
                width, height = source.size
                source.verify()
        except OriginalImageError:
            raise
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
            raise OriginalImageError("原图损坏或无法识别") from exc

        image = CellImage(
            name=f"ID_{digest[:32].upper()}", index=len(self.images) + 1,
            extension=extension, content_type=content_type, width=width, height=height,
        )
        # 不调用 save/resize/convert/crop，也不复用模型切片；ZIP 内就是原文件字节。
        self.archive.writestr(f"xl/media/{image.filename}", raw, compress_type=zipfile.ZIP_STORED)
        self.images[digest] = image
        return image

    def content_types_xml(self) -> str:
        if not self.images:
            return ""
        formats = sorted({(image.extension, image.content_type) for image in self.images.values()})
        return "".join(
            f'<Default Extension="{extension}" ContentType="{content_type}"/>'
            for extension, content_type in formats
        ) + f'<Override PartName="/xl/cellimages.xml" ContentType="{CELL_IMAGE_CONTENT_TYPE}"/>'

    def write_parts(self) -> None:
        if not self.images:
            return
        pictures, relationships = [], []
        for image in self.images.values():
            pictures.append(
                '<etc:cellImage><xdr:pic><xdr:nvPicPr>'
                f'<xdr:cNvPr id="{image.index}" name="{image.name}" '
                f'descr="完整原始图片（{image.width} × {image.height}）"/>'
                '<xdr:cNvPicPr><a:picLocks noChangeAspect="1"/></xdr:cNvPicPr></xdr:nvPicPr>'
                f'<xdr:blipFill><a:blip r:embed="rId{image.index}"/>'
                '<a:stretch><a:fillRect/></a:stretch></xdr:blipFill><xdr:spPr><a:xfrm>'
                '<a:off x="0" y="0"/>'
                f'<a:ext cx="{image.width * 9525}" cy="{image.height * 9525}"/>'
                '</a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
                '</xdr:spPr></xdr:pic></etc:cellImage>'
            )
            relationships.append(
                f'<Relationship Id="rId{image.index}" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                f'Target="media/{image.filename}"/>'
            )
        self.archive.writestr(
            "xl/cellimages.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<etc:cellImages xmlns:etc="http://www.wps.cn/officeDocument/2017/etCustomData" '
            'xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            + "".join(pictures) + '</etc:cellImages>',
        )
        self.archive.writestr(
            "xl/_rels/cellimages.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(relationships) + '</Relationships>',
        )
