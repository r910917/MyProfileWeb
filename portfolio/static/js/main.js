document.addEventListener("DOMContentLoaded", () => {
  const path = window.location.pathname;

  
  const loader = document.getElementById("loading");
  if (loader) {
    setTimeout(() => loader.classList.add("hidden"), 500);
  }

  // === 導覽列收合 ===
  const navLinks = document.querySelectorAll(".navbar-nav .nav-link");
  const navbarCollapse = document.getElementById("navbarNav"); // 用 id 抓 collapse

  navLinks.forEach(link => {
    link.addEventListener("click", () => {
      if (navbarCollapse.classList.contains("show")) {
        const collapse = bootstrap.Collapse.getInstance(navbarCollapse) 
                      || new bootstrap.Collapse(navbarCollapse, { toggle: false });
        collapse.hide();
      }
    });
  });

  // === 回到頂部按鈕 ===
  const backToTopBtn = document.getElementById("back-to-top");
  if (backToTopBtn) {
    window.addEventListener("scroll", () => {
      backToTopBtn.style.display = window.scrollY > 200 ? "block" : "none";
    });
    backToTopBtn.addEventListener("click", () => {
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  }

  // === 導覽列背景切換 ===
  document.addEventListener("scroll", () => {
    const nav = document.getElementById("mainNav");
    if (window.scrollY > 50) {
      nav.classList.add("navbar-scrolled");
    } else {
      nav.classList.remove("navbar-scrolled");
    }
  });


  // === 頁面進場動畫 ===
  document.body.classList.add("fade-in");
  const links = document.querySelectorAll("a.nav-link, a.navbar-brand");
  links.forEach(link => {
    link.addEventListener("click", (e) => {
      const href = link.getAttribute("href");
      if (href.startsWith("http") || href.startsWith("#")) return;
      e.preventDefault();
      document.body.classList.remove("fade-in");
      document.body.classList.add("fade-out");
      setTimeout(() => window.location.href = href, 400);
    });
  });

  // === 首頁效果 ===
  if (path === "/" || path === "/home/") {
    const banner = document.querySelector("h1");
    if (banner) {
      banner.addEventListener("mouseover", () => {
        banner.style.transform = "scale(1.1)";
        banner.style.transition = "0.3s";
      });
      banner.addEventListener("mouseout", () => {
        banner.style.transform = "scale(1)";
      });
    }
  }

  // === 首頁文字淡入 ===
  const introText = document.getElementById("intro-text");
  if (introText) {
    setTimeout(() => introText.classList.add("show"), 300);
  }

  // === 專案卡片顯示效果 ===
  const projectCards = document.querySelectorAll(".project-card");
  const revealProjects = () => {
    projectCards.forEach(card => {
      const rect = card.getBoundingClientRect();
      if (rect.top < window.innerHeight - 50) card.classList.add("visible");
    });
  };
  window.addEventListener("scroll", revealProjects);
  revealProjects();

  // === 進度條動畫 ===
  const progressBars = document.querySelectorAll(".progress-bar");
  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        const bar = entry.target;
        const targetWidth = parseInt(bar.getAttribute("data-width"));
        let current = 0;
        const label = bar.querySelector(".progress-label");

        const interval = setInterval(() => {
          if (current >= targetWidth) {
            clearInterval(interval);
            current = targetWidth;
          }
          bar.style.width = current + "%";
          if (label) label.textContent = current + "%";
          current++;
        }, 15);

        observer.unobserve(bar);
      }
    });
  }, { threshold: 0.5 });
  progressBars.forEach(bar => observer.observe(bar));

  // === 作品集篩選 ===
  const filterBtns = document.querySelectorAll(".filter-btn");
  const items = document.querySelectorAll(".portfolio-item");
  filterBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      const filter = btn.getAttribute("data-filter");
      items.forEach(item => {
        item.style.display = (filter === "all" || item.classList.contains(filter)) ? "block" : "none";
      });
    });
  });

  // === 作品集卡片動畫 ===
  if (path.includes("/portfolio")) {
    const cards = document.querySelectorAll(".card");
    cards.forEach(card => {
      card.style.opacity = 0;
      card.style.transform = "translateY(30px)";
      card.style.transition = "all 0.6s ease-out";
    });
    const revealOnScroll = () => {
      cards.forEach(card => {
        const rect = card.getBoundingClientRect();
        if (rect.top < window.innerHeight - 50) {
          card.style.opacity = 1;
          card.style.transform = "translateY(0)";
        }
      });
    };
    window.addEventListener("scroll", revealOnScroll);
    revealOnScroll();
  }

  // === 聯絡我：按鈕送出狀態 ===
  if (path.includes("/contact")) {
    const form = document.querySelector("form");
    const button = document.querySelector("button[type='submit']");
    if (form && button) {
      form.addEventListener("submit", () => {
        button.innerText = "已送出 ✅";
        button.style.backgroundColor = "gray";
        button.disabled = true;
      });
    }
  }

  // === 關於我：標題淡入 ===
  if (path.includes("/about")) {
    const title = document.querySelector("h2");
    if (title) {
      title.style.opacity = 0;
      title.style.transition = "opacity 2s";
      setTimeout(() => title.style.opacity = 1, 200);
    }
  }
});

// === Minecraft 排行榜查詢 ===
document.querySelectorAll(".rank-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    const type = btn.getAttribute("data-type");
    fetch(`/minecraft/rank/?type=${type}`)
      .then(res => res.json())
      .then(data => {
        let html = `<h3 class="mt-4">排行榜 - ${btn.textContent}</h3>`;
        html += "<table class='table table-striped' id='rankTable'><thead><tr>";

        if (type === "money") {
          html += "<th>#</th><th>玩家</th><th>金幣</th></tr></thead><tbody>";
          data.rows.forEach((row, i) => {
            html += `<tr><td>${i+1}</td><td>${row.player_name}</td><td>${row.money}</td></tr>`;
          });
        } else if (type === "level") {
          html += "<th>#</th><th>UUID</th><th>職業</th><th>主等級</th></tr></thead><tbody>";
          data.rows.forEach((row, i) => {
            html += `<tr><td>${i+1}</td><td>${row.uuid}</td><td>${row.class}</td><td>${row.mainlevel_level}</td></tr>`;
          });
        } else if (type === "guild") {
          html += "<th>#</th><th>公會名稱</th><th>等級</th><th>資金</th></tr></thead><tbody>";
          data.rows.forEach((row, i) => {
            html += `<tr><td>${i+1}</td><td>${row.gname}</td><td>${row.glevel}</td><td>${row.gmoney}</td></tr>`;
          });
        } else if (type === "playtime") {
          html += "<th>#</th><th>玩家</th><th>遊玩時間</th><th>餘額</th></tr></thead><tbody>";
          data.rows.forEach((row, i) => {
            html += `<tr><td>${i+1}</td><td>${row.username}</td><td>${row.TotalPlayTime}</td><td>${row.Balance}</td></tr>`;
          });
        }

        html += "</tbody></table>";
        document.getElementById("results").innerHTML = html;
        document.getElementById("rankSearchBox").style.display = "flex";
      });
  });
});

// === Minecraft 即時搜尋 ===
document.getElementById("rankSearchInput")?.addEventListener("input", function() {
  const filter = this.value.toLowerCase();
  const rows = document.querySelectorAll("#rankTable tbody tr");
  rows.forEach(row => {
    const text = row.innerText.toLowerCase();
    row.style.display = text.includes(filter) ? "" : "none";
    row.style.backgroundColor = text.includes(filter) ? "#fff3cd" : "";
  });
});
