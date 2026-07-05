import sys, os
from ScaFFold.utils.data_loading import BasicDataset
dsroot = sys.argv[1]
for split in ("training", "validation"):
    images_dir = os.path.join(dsroot, "volumes", split)
    mask_dir = os.path.join(dsroot, "masks", split)
    if not os.path.isdir(images_dir):
        print(f"skip {split}: {images_dir} missing"); continue
    out_dir = os.path.join(dsroot, "packed")
    n = BasicDataset.pack_split(images_dir, mask_dir, "_mask", out_dir, split)
    print(f"packed {split}: {n} samples -> {out_dir}/images_{split}.npy")
print("PACK_DONE")
