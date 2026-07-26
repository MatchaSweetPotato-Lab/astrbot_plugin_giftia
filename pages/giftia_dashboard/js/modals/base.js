// Giftia Dashboard Modals - Base Utilities

window.openModal = function(id) {
    const modal = document.getElementById(id);
    if (modal) {
        modal.classList.add("show");
        document.body.classList.add("modal-open");
    }
};

window.closeModal = function(id) {
    const modal = document.getElementById(id);
    if (modal) {
        modal.classList.remove("show");

        // 暂停并重置该弹窗内部的所有音视频播放器，防止背景声音继续播放
        modal.querySelectorAll("video, audio").forEach(mediaEl => {
            try {
                mediaEl.pause();
                mediaEl.removeAttribute("src");
                mediaEl.load();
            } catch (e) {}
        });

        if (id === "edit-media-modal") {
            const previewBox = document.getElementById("edit-media-preview");
            if (previewBox) {
                previewBox.innerHTML = "";
            }
        }

        // Only remove modal-open class if no other modals are open
        const openedModals = document.querySelectorAll(".modal-overlay.show");
        if (openedModals.length === 0) {
            document.body.classList.remove("modal-open");
        }
    }
};

// Add listener to close modal when clicking outside (on overlay)
document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".modal-overlay").forEach(overlay => {
        overlay.addEventListener("click", function(e) {
            // Close if clicking directly on the overlay backdrop
            if (e.target === this) {
                const id = this.id;
                if (id) {
                    window.closeModal(id);
                }
            }
        });
    });
});
