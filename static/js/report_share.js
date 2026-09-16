/**
 * Universal Report Share Utility for P&M Portal
 * Supports 1-click sharing of real PDF/Excel documents AND direct links to WhatsApp, Email, etc.
 */

(function() {
    let currentShareConfig = {
        title: 'P&M Project Report',
        subtitle: 'GIAP Gelephu Site',
        excelUrl: '',
        pdfUrl: '',
        activeFormat: 'pdf'
    };

    const DASHBOARD_CARD_REPORTS = {
        'vehicle_profiles': {
            title: 'Vehicle Profiles & Fleet 360° Directory',
            subtitle: 'P&M Vehicle Directory & Lifecycle Report',
            pdfUrl: '/fleet/vehicles/export-pdf/',
            excelUrl: '/fleet/vehicles/export-excel/',
            defaultFormat: 'pdf'
        },
        'breakdown_register': {
            title: 'Workshop & Breakdown Register Report',
            subtitle: 'P&M Fleet Repairs & Maintenance Log',
            pdfUrl: '/fleet/vehicles/export-pdf/?status=workshop',
            excelUrl: '/fleet/vehicles/export-excel/?status=workshop',
            defaultFormat: 'pdf'
        },
        'camp': {
            title: 'Camp Security & Resident Report',
            subtitle: 'GIAP Gelephu Camp Management',
            pdfUrl: '/camp/export-pdf/',
            excelUrl: '/camp/export-excel/',
            defaultFormat: 'pdf'
        },
        'mess': {
            title: 'Mess Management & Meals Report',
            subtitle: 'GIAP Gelephu Mess System',
            pdfUrl: '/mess/export-pdf/',
            excelUrl: '/mess/export-excel/',
            defaultFormat: 'pdf'
        },
        'overtime': {
            title: 'Overtime & Time Keeper Report',
            subtitle: 'GIAP Gelephu Overtime Records',
            pdfUrl: '/overtime/export-pdf/',
            excelUrl: '/overtime/export-excel/',
            defaultFormat: 'pdf'
        },
        'daily_deployment': {
            title: 'Daily Deployment Report',
            subtitle: 'P&M Fleet & Machinery Allocation',
            pdfUrl: '/deployment/export-allocation-pdf/',
            excelUrl: '/deployment/export-excel/',
            defaultFormat: 'pdf'
        },
        'shift_management': {
            title: 'Shift Allocation Report',
            subtitle: 'P&M Shift Allocation & Rosters',
            pdfUrl: '/api/shifts/export/?format=pdf',
            excelUrl: '/api/shifts/export/?format=excel',
            defaultFormat: 'pdf'
        },
        'vehicle_movement': {
            title: 'Vehicle Movement Register',
            subtitle: 'P&M Fleet Movements & Trips',
            pdfUrl: '/fleet/movements/export/?format=pdf',
            excelUrl: '/fleet/movements/export/?format=excel',
            defaultFormat: 'pdf'
        },
        'tyre_section': {
            title: 'Tyre Inspection & Cost Report',
            subtitle: 'P&M Fleet Tyre Section',
            pdfUrl: '/fleet/api/tyre/export-pdf/',
            excelUrl: '/fleet/api/tyre/export/',
            defaultFormat: 'pdf'
        },
        'total_employees': {
            title: 'Master Employee Directory',
            subtitle: 'P&M Workforce Records',
            pdfUrl: '/api/employees/export-pdf/',
            excelUrl: '/api/employees/export-excel/',
            defaultFormat: 'pdf'
        },
        'foreign_workers': {
            title: 'Foreign Workers Report',
            subtitle: 'Expatriate Workforce Records',
            pdfUrl: '/api/employees/export-pdf/?category=foreign',
            excelUrl: '/api/employees/export-excel/?category=foreign',
            defaultFormat: 'pdf'
        },
        'national_workers': {
            title: 'National Workers Report',
            subtitle: 'Bhutanese Workforce Records',
            pdfUrl: '/api/employees/export-pdf/?category=national',
            excelUrl: '/api/employees/export-excel/?category=national',
            defaultFormat: 'pdf'
        },
        'attendance': {
            title: 'Attendance & Muster Roll Report',
            subtitle: 'P&M Workforce Attendance',
            pdfUrl: '/attendance/export/pdf/',
            excelUrl: '/attendance/export/muster-excel/',
            defaultFormat: 'pdf'
        },
        'total_vehicles': {
            title: 'Master Fleet Vehicle Report',
            subtitle: 'P&M Machinery & Vehicle Fleet',
            pdfUrl: '/fleet/vehicles/export-pdf/',
            excelUrl: '/fleet/vehicles/export-excel/',
            defaultFormat: 'pdf'
        },
        'lubricants': {
            title: 'Lubricant Consumption Report',
            subtitle: 'P&M Lubrication Log',
            pdfUrl: '/fleet/api/lubrication/export-pdf/',
            excelUrl: '/fleet/api/lubrication/export/',
            defaultFormat: 'pdf'
        },
        'spare_parts': {
            title: 'Spare Parts Inventory Report',
            subtitle: 'P&M Warehouse & Stock Records',
            pdfUrl: '/fleet/api/spare-parts/export-pdf/',
            excelUrl: '/fleet/api/spare-parts/export/',
            defaultFormat: 'pdf'
        },
        'document_expiries': {
            title: 'Document Expiries & Insurance Alert Report',
            subtitle: 'Vehicle RC, Insurance & Permits',
            pdfUrl: '/documents/insurance/export-pdf/',
            excelUrl: '/documents/insurance/export-excel/',
            defaultFormat: 'pdf'
        },
        'manage_logins': {
            title: 'User Logins & Security Audit Report',
            subtitle: 'System Access & Roles',
            pdfUrl: '/user-activity/export-pdf/',
            excelUrl: '/user-activity/export/',
            defaultFormat: 'pdf'
        }
    };

    function getAbsoluteUrl(relativeOrAbsolute) {
        if (!relativeOrAbsolute) return window.location.href;
        if (relativeOrAbsolute.startsWith('http://') || relativeOrAbsolute.startsWith('https://')) {
            return relativeOrAbsolute;
        }
        return window.location.origin + (relativeOrAbsolute.startsWith('/') ? '' : '/') + relativeOrAbsolute;
    }

    function getFormattedDateStr() {
        const now = new Date();
        return now.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' });
    }

    function buildShareMessage(format, url, title) {
        const dateStr = getFormattedDateStr();
        const fmtLabel = format.toUpperCase();
        const absUrl = getAbsoluteUrl(url);
        
        return `📋 *P&M Department — GIAP Gelephu*\n` +
               `📊 *Report:* ${title} (${fmtLabel})\n` +
               `🗓️ *Date:* ${dateStr}\n\n` +
               `🔗 *Direct Download / View Document:*\n${absUrl}\n\n` +
               `_Generated via P&M Management Portal_`;
    }

    function showShareToast(message, isError) {
        let toast = document.getElementById('reportShareToast');
        if (!toast) {
            toast = document.createElement('div');
            toast.id = 'reportShareToast';
            toast.style.cssText = `
                position: fixed;
                bottom: 24px;
                left: 50%;
                transform: translateX(-50%) translateY(100px);
                background: #1e293b;
                color: #ffffff;
                padding: 12px 24px;
                border-radius: 30px;
                font-size: 0.88rem;
                font-weight: 700;
                box-shadow: 0 10px 25px rgba(0,0,0,0.3);
                z-index: 999999;
                transition: transform 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275);
                display: flex;
                align-items: center;
                gap: 8px;
            `;
            document.body.appendChild(toast);
        }
        toast.style.background = isError ? '#ef4444' : '#10b981';
        toast.innerHTML = (isError ? '⚠️ ' : '✅ ') + message;
        toast.style.transform = 'translateX(-50%) translateY(0)';
        setTimeout(() => {
            toast.style.transform = 'translateX(-50%) translateY(100px)';
        }, 3200);
    }

    function triggerDirectDownload(url, filename) {
        const absUrl = getAbsoluteUrl(url);
        const a = document.createElement('a');
        a.href = absUrl;
        if (filename) a.download = filename;
        a.target = '_blank';
        document.body.appendChild(a);
        a.click();
        setTimeout(() => {
            if (a.parentNode) document.body.removeChild(a);
        }, 300);
    }

    /**
     * Converts an HTML print view string into a genuine binary PDF Blob (%PDF-1.4)
     * using html2pdf.js so WhatsApp, Adobe Acrobat and all viewers can open it directly.
     */
    async function convertHtmlToPdfBlob(htmlText, title, orientation) {
        if (typeof html2pdf === 'undefined') {
            console.warn('html2pdf library is not defined, returning text blob');
            return new Blob([htmlText], { type: 'text/html' });
        }

        const isLandscape = orientation !== 'portrait';
        const containerWidth = isLandscape ? 1120 : 790;

        // Create an offscreen wrapper that prevents layout shift on the main page
        // while keeping elements at standard non-negative coordinates in DOM
        const sandbox = document.createElement('div');
        sandbox.id = 'pdf-render-sandbox-wrapper';
        sandbox.style.cssText = `
            position: fixed;
            top: 0;
            left: 0;
            width: ${containerWidth}px;
            height: 100vh;
            overflow: hidden;
            opacity: 0;
            pointer-events: none;
            z-index: -999999;
        `;

        const container = document.createElement('div');
        container.id = 'pdf-render-sandbox';
        container.className = 'pdf-render-content';
        container.style.cssText = `
            position: relative;
            width: ${containerWidth}px;
            background: #ffffff;
            color: #000000;
            padding: 16px;
            box-sizing: border-box;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        `;

        const parser = new DOMParser();
        const doc = parser.parseFromString(htmlText, 'text/html');

        // Remove navigation bars, action buttons and auto-print scripts
        doc.querySelectorAll('.no-print, button, script').forEach(el => el.remove());

        // Copy body HTML
        container.innerHTML = doc.body ? doc.body.innerHTML : htmlText;

        // Copy and adapt style tags (adapt 'body' selector to target our container as well)
        doc.querySelectorAll('style').forEach(st => {
            let css = st.textContent || '';
            css = css.replace(/(^|[,\s}])body(?=[,\s{])/g, '$1.pdf-render-content');
            const newStyle = document.createElement('style');
            newStyle.textContent = css;
            container.appendChild(newStyle);
        });

        // Also carry over external stylesheets (<link rel="stylesheet">) if any
        doc.querySelectorAll('link[rel="stylesheet"]').forEach(link => {
            container.appendChild(link.cloneNode(true));
        });

        sandbox.appendChild(container);
        document.body.appendChild(sandbox);

        // Wait for all images to finish loading/decoding
        const images = Array.from(container.querySelectorAll('img'));
        if (images.length > 0) {
            await Promise.all(images.map(img => {
                if (img.complete && img.naturalHeight !== 0) return Promise.resolve();
                return new Promise(res => {
                    img.onload = res;
                    img.onerror = res;
                    setTimeout(res, 600); // safe fallback
                });
            }));
        }

        // Brief delay for DOM reflow & font layout
        await new Promise(r => setTimeout(r, 150));

        const opt = {
            margin: [6, 6, 6, 6],
            filename: `${title}.pdf`,
            image: { type: 'jpeg', quality: 0.98 },
            enableLinks: false,
            html2canvas: {
                scale: 2,
                useCORS: true,
                logging: false,
                allowTaint: true,
                scrollX: 0,
                scrollY: 0,
                x: 0,
                y: 0,
                windowWidth: containerWidth
            },
            jsPDF: {
                unit: 'mm',
                format: 'a4',
                orientation: isLandscape ? 'landscape' : 'portrait'
            }
        };

        try {
            const pdfBlob = await html2pdf().set(opt).from(container).outputPdf('blob');
            return pdfBlob;
        } finally {
            if (sandbox.parentNode) {
                sandbox.parentNode.removeChild(sandbox);
            }
        }
    }

    /**
     * Fetches and validates document from server. Uses server-side native PDF renderer
     * to produce authentic, crisp vector %PDF-1.4 documents with real selectable text.
     */
    async function getRealDocumentFile(format, url, title, orientation) {
        const ext = format === 'excel' ? 'xlsx' : 'pdf';
        const mimeType = format === 'excel' ? 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' : 'application/pdf';
        const safeTitle = (title || 'Report').replace(/[^a-zA-Z0-9_-]/g, '_');
        const fileName = `${safeTitle}_${new Date().toISOString().slice(0, 10)}.${ext}`;

        if (format === 'excel') {
            const absUrl = getAbsoluteUrl(url);
            const resp = await fetch(absUrl, { credentials: 'same-origin' });
            if (!resp.ok) throw new Error('Document fetch failed with status ' + resp.status);
            const blob = await resp.blob();
            return new File([blob], fileName, { type: mimeType });
        }

        // Priority 1: Request authentic Native Vector PDF (%PDF-1.4) directly from server
        try {
            showShareToast('⏳ Generating official Vector PDF...');
            const nativeEndpoint = `/api/reports/native-pdf/?url=${encodeURIComponent(url)}&title=${encodeURIComponent(safeTitle)}`;
            const nativeResp = await fetch(nativeEndpoint, { credentials: 'same-origin' });
            if (nativeResp.ok) {
                const ct = nativeResp.headers.get('content-type') || '';
                if (ct.includes('application/pdf')) {
                    const blob = await nativeResp.blob();
                    if (blob.size > 500) {
                        return new File([blob], fileName, { type: 'application/pdf' });
                    }
                }
            }
        } catch (nativeErr) {
            console.warn('Native PDF server endpoint error, falling back:', nativeErr);
        }

        // Fallback: Fetch direct document URL
        const absUrl = getAbsoluteUrl(url);
        const resp = await fetch(absUrl, { credentials: 'same-origin' });
        if (!resp.ok) throw new Error('Document fetch failed with status ' + resp.status);

        const arrayBuffer = await resp.arrayBuffer();
        const uint8 = new Uint8Array(arrayBuffer);

        const isBinaryPdf = uint8.length >= 4 &&
                            uint8[0] === 0x25 && // %
                            uint8[1] === 0x50 && // P
                            uint8[2] === 0x44 && // D
                            uint8[3] === 0x46;   // F

        if (isBinaryPdf) {
            const blob = new Blob([arrayBuffer], { type: 'application/pdf' });
            return new File([blob], fileName, { type: 'application/pdf' });
        }

        // Fallback compilation
        showShareToast('⏳ Compiling official PDF document...');
        const decoder = new TextDecoder('utf-8');
        const htmlText = decoder.decode(uint8);
        const realPdfBlob = await convertHtmlToPdfBlob(htmlText, safeTitle, orientation);
        return new File([realPdfBlob], fileName, { type: 'application/pdf' });
    }

    window.openOfficialPrintView = function() {
        const format = currentShareConfig.activeFormat;
        const url = format === 'excel' ? currentShareConfig.excelUrl : currentShareConfig.pdfUrl;
        if (!url) {
            showShareToast('Document view not available for this module.', true);
            return;
        }
        window.open(getAbsoluteUrl(url), '_blank');
    };

    window.setShareDatePreset = function(preset) {
        const dateFromEl = document.getElementById('shareModalDateFrom');
        const dateToEl = document.getElementById('shareModalDateTo');
        const badge = document.getElementById('shareModalActiveFilterBadge');
        const today = new Date();
        const formatDate = d => d.toISOString().split('T')[0];

        if (preset === 'today') {
            const tStr = formatDate(today);
            if (dateFromEl) dateFromEl.value = tStr;
            if (dateToEl) dateToEl.value = tStr;
            if (badge) badge.textContent = 'Today';
        } else if (preset === 'last7') {
            const past = new Date(today);
            past.setDate(past.getDate() - 7);
            if (dateFromEl) dateFromEl.value = formatDate(past);
            if (dateToEl) dateToEl.value = formatDate(today);
            if (badge) badge.textContent = 'Last 7 Days';
        } else if (preset === 'thisMonth') {
            const firstDay = new Date(today.getFullYear(), today.getMonth(), 1);
            if (dateFromEl) dateFromEl.value = formatDate(firstDay);
            if (dateToEl) dateToEl.value = formatDate(today);
            if (badge) badge.textContent = 'This Month';
        } else if (preset === 'all') {
            if (dateFromEl) dateFromEl.value = '';
            if (dateToEl) dateToEl.value = '';
            if (badge) badge.textContent = 'All Time';
        }
        updateShareModalFilters();
    };

    window.updateShareModalFilters = function() {
        const dateFrom = document.getElementById('shareModalDateFrom')?.value || '';
        const dateTo = document.getElementById('shareModalDateTo')?.value || '';
        const searchVal = (document.getElementById('shareModalSearch')?.value || '').trim();

        const buildFilteredUrl = (baseUrl) => {
            if (!baseUrl) return '';
            const abs = getAbsoluteUrl(baseUrl);
            try {
                const urlObj = new URL(abs);
                if (dateFrom) urlObj.searchParams.set('date_from', dateFrom);
                else urlObj.searchParams.delete('date_from');

                if (dateTo) urlObj.searchParams.set('date_to', dateTo);
                else urlObj.searchParams.delete('date_to');

                if (searchVal) {
                    urlObj.searchParams.set('search', searchVal);
                    urlObj.searchParams.set('q', searchVal);
                } else {
                    urlObj.searchParams.delete('search');
                    urlObj.searchParams.delete('q');
                }
                return urlObj.pathname + urlObj.search;
            } catch(e) {
                return baseUrl;
            }
        };

        currentShareConfig.pdfUrl = buildFilteredUrl(currentShareConfig.basePdfUrl);
        currentShareConfig.excelUrl = buildFilteredUrl(currentShareConfig.baseExcelUrl);

        selectShareFormat(currentShareConfig.activeFormat);
    };

    window.openReportShareModal = function(options) {
        currentShareConfig = {
            title: options.title || 'Project Report',
            subtitle: options.subtitle || 'GIAP Gelephu Site',
            basePdfUrl: options.pdfUrl || '',
            baseExcelUrl: options.excelUrl || '',
            excelUrl: options.excelUrl || '',
            pdfUrl: options.pdfUrl || '',
            orientation: options.orientation || 'landscape',
            activeFormat: options.defaultFormat || (options.pdfUrl ? 'pdf' : (options.excelUrl ? 'excel' : 'pdf'))
        };

        const modal = document.getElementById('universalReportShareModal');
        if (!modal) {
            console.error('Universal report share modal element not found in DOM.');
            return;
        }

        const titleEl = document.getElementById('shareModalReportTitle');
        const subEl = document.getElementById('shareModalReportSub');
        const excelTab = document.getElementById('shareTabExcel');
        const pdfTab = document.getElementById('shareTabPdf');
        const dateFromEl = document.getElementById('shareModalDateFrom');
        const dateToEl = document.getElementById('shareModalDateTo');
        const searchEl = document.getElementById('shareModalSearch');
        const badge = document.getElementById('shareModalActiveFilterBadge');

        if (titleEl) titleEl.innerText = currentShareConfig.title;
        if (subEl) subEl.innerText = currentShareConfig.subtitle;

        // Reset modal filters on open
        if (dateFromEl) dateFromEl.value = '';
        if (dateToEl) dateToEl.value = '';
        if (searchEl) searchEl.value = '';
        if (badge) badge.textContent = 'All Time';

        // Toggle visibility of format tabs depending on available URLs
        if (excelTab) {
            excelTab.style.display = currentShareConfig.excelUrl ? 'flex' : 'none';
        }
        if (pdfTab) {
            pdfTab.style.display = currentShareConfig.pdfUrl ? 'flex' : 'none';
        }

        selectShareFormat(currentShareConfig.activeFormat);
        modal.style.display = 'flex';
    };

    window.shareDashboardCard = function(cardKey) {
        const config = DASHBOARD_CARD_REPORTS[cardKey];
        if (!config) {
            console.warn('No report configuration found for card:', cardKey);
            return;
        }
        openReportShareModal(config);
    };

    window.closeReportShareModal = function() {
        const modal = document.getElementById('universalReportShareModal');
        if (modal) modal.style.display = 'none';
    };

    window.selectShareFormat = function(fmt) {
        currentShareConfig.activeFormat = fmt;
        const excelTab = document.getElementById('shareTabExcel');
        const pdfTab = document.getElementById('shareTabPdf');

        if (fmt === 'excel') {
            if (excelTab) excelTab.classList.add('active');
            if (pdfTab) pdfTab.classList.remove('active');
        } else {
            if (pdfTab) pdfTab.classList.add('active');
            if (excelTab) excelTab.classList.remove('active');
        }

        const previewLink = document.getElementById('shareModalLinkPreview');
        const activeUrl = fmt === 'excel' ? currentShareConfig.excelUrl : currentShareConfig.pdfUrl;
        if (previewLink) {
            previewLink.innerText = getAbsoluteUrl(activeUrl);
        }
    };

    window.shareToWhatsApp = async function(overrideFormat, overrideUrl, overrideTitle) {
        const format = overrideFormat || currentShareConfig.activeFormat;
        const url = overrideUrl || (format === 'excel' ? currentShareConfig.excelUrl : currentShareConfig.pdfUrl);
        const title = overrideTitle || currentShareConfig.title;
        const orientation = currentShareConfig.orientation || 'landscape';

        if (!url) {
            showShareToast('Report document is not available for this module.', true);
            return;
        }

        const absUrl = getAbsoluteUrl(url);
        const message = buildShareMessage(format, absUrl, title);

        showShareToast('Preparing ' + format.toUpperCase() + ' document...');

        try {
            // Fetch or compile genuine binary document file (PDF or Excel)
            const file = await getRealDocumentFile(format, url, title, orientation);

            // Try Web Share API Level 2 (Direct binary file sharing with contacts)
            if (navigator.share && navigator.canShare && navigator.canShare({ files: [file] })) {
                await navigator.share({
                    files: [file],
                    title: `${title} (${format.toUpperCase()})`,
                    text: message
                });
                showShareToast('Document file and link shared successfully!');
                return;
            }

            // Fallback for Desktop / WhatsApp Web:
            // 1. Download the real binary file directly
            const blobUrl = URL.createObjectURL(file);
            triggerDirectDownload(blobUrl, file.name);
            setTimeout(() => URL.revokeObjectURL(blobUrl), 15000);

            // 2. Open WhatsApp Web with the pre-formatted report message and link
            const waUrl = `https://api.whatsapp.com/send?text=${encodeURIComponent(message)}`;
            window.open(waUrl, '_blank');

            showShareToast('📥 ' + format.toUpperCase() + ' downloaded & WhatsApp opened! (Attach file in chat)');
        } catch (err) {
            if (err.name === 'AbortError') {
                return; // User cancelled share sheet
            }
            console.error('Document preparation error:', err);
            // Fallback to WhatsApp Web link
            const waUrl = `https://api.whatsapp.com/send?text=${encodeURIComponent(message)}`;
            window.open(waUrl, '_blank');
            showShareToast('WhatsApp opened with direct report link.');
        }
    };

    window.shareToEmail = function(overrideFormat, overrideUrl, overrideTitle) {
        const format = overrideFormat || currentShareConfig.activeFormat;
        const url = overrideUrl || (format === 'excel' ? currentShareConfig.excelUrl : currentShareConfig.pdfUrl);
        const title = overrideTitle || currentShareConfig.title;

        if (!url) {
            showShareToast('Report document is not available.', true);
            return;
        }

        const absUrl = getAbsoluteUrl(url);
        const subject = `[P&M GIAP Report] ${title} (${format.toUpperCase()}) - ${getFormattedDateStr()}`;
        const body = `Dear Sir/Madam,\n\nPlease find the generated P&M Department report details below:\n\n` +
                     `Report Name: ${title}\n` +
                     `Format: ${format.toUpperCase()}\n` +
                     `Project Site: GIAP Gelephu\n` +
                     `Date Generated: ${getFormattedDateStr()}\n\n` +
                     `Direct Download / View Document:\n${absUrl}\n\n` +
                     `Regards,\nP&M Project Team\nGIAP Gelephu`;

        const mailtoUrl = `mailto:?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
        window.location.href = mailtoUrl;
        showShareToast('Opening Email client...');
    };

    window.copyReportLink = function(overrideFormat, overrideUrl) {
        const format = overrideFormat || currentShareConfig.activeFormat;
        const url = overrideUrl || (format === 'excel' ? currentShareConfig.excelUrl : currentShareConfig.pdfUrl);

        if (!url) {
            showShareToast('Report link not available.', true);
            return;
        }

        const absUrl = getAbsoluteUrl(url);
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(absUrl).then(() => {
                showShareToast('Direct document link copied to clipboard!');
            }).catch(() => {
                fallbackCopy(absUrl);
            });
        } else {
            fallbackCopy(absUrl);
        }
    };

    function fallbackCopy(text) {
        const inp = document.createElement('input');
        inp.value = text;
        document.body.appendChild(inp);
        inp.select();
        try {
            document.execCommand('copy');
            showShareToast('Direct document link copied to clipboard!');
        } catch (e) {
            showShareToast('Could not copy link.', true);
        }
        document.body.removeChild(inp);
    }

    window.downloadReportCurrent = async function() {
        const format = currentShareConfig.activeFormat;
        const url = format === 'excel' ? currentShareConfig.excelUrl : currentShareConfig.pdfUrl;
        const title = currentShareConfig.title || 'Report';
        const orientation = currentShareConfig.orientation || 'landscape';

        if (!url) {
            showShareToast('Document not available.', true);
            return;
        }

        showShareToast('Generating ' + format.toUpperCase() + ' file...');
        try {
            const file = await getRealDocumentFile(format, url, title, orientation);
            const blobUrl = URL.createObjectURL(file);
            triggerDirectDownload(blobUrl, file.name);
            setTimeout(() => URL.revokeObjectURL(blobUrl), 15000);
            showShareToast('📥 ' + file.name + ' downloaded successfully!');
        } catch (err) {
            console.error('Download error:', err);
            window.open(getAbsoluteUrl(url), '_blank');
        }
    };
})();
