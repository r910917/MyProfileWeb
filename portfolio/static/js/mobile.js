document.addEventListener("DOMContentLoaded", () => {
  function isMobile() {
    return window.innerWidth <= 768;
  }

  // 字體大小
  function adjustFontSize() {
    document.body.style.fontSize = isMobile() ? "14px" : "16px";
  }

  // 區塊間距
  function adjustSpacing() {
    const sections = document.querySelectorAll(".section-box");
    sections.forEach(sec => {
      sec.style.margin = isMobile() ? "20px 0" : "40px 0";
      sec.style.padding = isMobile() ? "15px" : "30px";
    });
  }

  // 圖片 / 卡片大小
  function adjustImages() {
    const imgs = document.querySelectorAll("img, .card");
    imgs.forEach(img => {
      img.style.maxWidth = isMobile() ? "100%" : "600px";
      img.style.margin = "0 auto"; // 手機板置中
    });
  }

  // 橫向模式提醒
  function checkOrientation() {
    if (window.orientation === 90 || window.orientation === -90) {
      alert("建議使用直式模式以獲得最佳體驗 📱");
    }
  }

  // 執行
  function applyMobileAdjustments() {
    adjustFontSize();
    adjustSpacing();
    adjustImages();
  }

  window.addEventListener("resize", applyMobileAdjustments);
  window.addEventListener("orientationchange", checkOrientation);

  applyMobileAdjustments();
});
