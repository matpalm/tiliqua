#![no_std]
#![no_main]

use critical_section::Mutex;
use core::convert::TryInto;
use log::info;
use riscv_rt::entry;
use irq::handler;
use core::cell::RefCell;
use heapless::String;

use core::str::FromStr;

use tiliqua_pac as pac;
use tiliqua_hal as hal;
use tiliqua_lib::*;
use tiliqua_lib::opt::*;
use tiliqua_lib::generated_constants::*;
use tiliqua_fw::*;
use tiliqua_lib::palette::ColorPalette;
use tiliqua_lib::manifest::*;

use embedded_graphics::{
    mono_font::{ascii::FONT_9X15, ascii::FONT_9X15_BOLD, MonoTextStyle},
    pixelcolor::{Gray8, GrayColor},
    prelude::*,
    primitives::{PrimitiveStyleBuilder, Line},
    text::{Alignment, Text},
};

use opts::Options;
use hal::pca9635::Pca9635Driver;

impl_ui!(UI,
         Options,
         Encoder0,
         Pca9635Driver<I2c0>,
         EurorackPmod0);

hal::impl_dma_display!(DMADisplay, H_ACTIVE, V_ACTIVE,
                       VIDEO_ROTATE_90);

pub const TIMER0_ISR_PERIOD_MS: u32 = 5;
pub const N_MANIFESTS: usize = 8;

struct App {
    ui: UI,
    reboot_n: Option<usize>,
    error_n: [Option<String<32>>; N_MANIFESTS],
    time_since_reboot_requested: u32,
    manifests: [Option<BitstreamManifest>; N_MANIFESTS],
}

impl App {
    pub fn new(opts: Options, manifests: [Option<BitstreamManifest>; N_MANIFESTS]) -> Self {
        let peripherals = unsafe { pac::Peripherals::steal() };
        let encoder = Encoder0::new(peripherals.ENCODER0);
        let i2cdev = I2c0::new(peripherals.I2C0);
        let pca9635 = Pca9635Driver::new(i2cdev);
        let pmod = EurorackPmod0::new(peripherals.PMOD0_PERIPH);
        Self {
            ui: UI::new(opts, TIMER0_ISR_PERIOD_MS,
                        encoder, pca9635, pmod),
            reboot_n: None,
            error_n: [const { None }; N_MANIFESTS],
            time_since_reboot_requested: 0u32,
            manifests,
        }
    }
}

fn print_rebooting<D>(d: &mut D, rng: &mut fastrand::Rng)
where
    D: DrawTarget<Color = Gray8>,
{
    let style = MonoTextStyle::new(&FONT_9X15_BOLD, Gray8::WHITE);
    Text::with_alignment(
        "REBOOTING",
        Point::new(rng.i32(0..H_ACTIVE as i32), rng.i32(0..V_ACTIVE as i32)),
        style,
        Alignment::Center,
    )
    .draw(d).ok();
}

fn draw_summary<D>(d: &mut D, bitstream: &BitstreamManifest, or: i32, ot: i32, hue: u8)
where
    D: DrawTarget<Color = Gray8>,
{
    let norm = MonoTextStyle::new(&FONT_9X15,      Gray8::new(0xB0 + hue));
    Text::with_alignment(
        "brief:".into(),
        Point::new((H_ACTIVE/2 - 10) as i32 + or, (V_ACTIVE/2+20) as i32 + ot),
        norm,
        Alignment::Right,
    )
    .draw(d).ok();
    Text::with_alignment(
        &bitstream.brief,
        Point::new((H_ACTIVE/2) as i32 + or, (V_ACTIVE/2+20) as i32 + ot),
        norm,
        Alignment::Left,
    )
    .draw(d).ok();
    Text::with_alignment(
        "video:".into(),
        Point::new((H_ACTIVE/2 - 10) as i32 + or, (V_ACTIVE/2+40) as i32 + ot),
        norm,
        Alignment::Right,
    )
    .draw(d).ok();
    Text::with_alignment(
        &bitstream.video,
        Point::new((H_ACTIVE/2) as i32 + or, (V_ACTIVE/2+40) as i32 + ot),
        norm,
        Alignment::Left,
    )
    .draw(d).ok();
    Text::with_alignment(
        "sha:".into(),
        Point::new((H_ACTIVE/2 - 10) as i32 + or, (V_ACTIVE/2+60) as i32 + ot),
        norm,
        Alignment::Right,
    )
    .draw(d).ok();
    Text::with_alignment(
        &bitstream.sha,
        Point::new((H_ACTIVE/2) as i32 + or, (V_ACTIVE/2+60) as i32 + ot),
        norm,
        Alignment::Left,
    )
    .draw(d).ok();
}

fn timer0_handler(app: &Mutex<RefCell<App>>) {

    critical_section::with(|cs| {

        let mut app = app.borrow_ref_mut(cs);

        //
        // Update UI and options
        //

        app.ui.update();

        if app.ui.opts.modify() {
            if let Some(n) = app.ui.opts.view().selected() {
                app.reboot_n = Some(n)
            }
        }

        if let Some(n) = app.reboot_n {
            app.time_since_reboot_requested += app.ui.period_ms;
            // Give codec time to mute and display time to draw 'REBOOTING'
            if app.time_since_reboot_requested > 500 {
                // Is there a firmware image to copy to PSRAM before we switch bitstreams?
                let mut bad_crc = false;
                if let Some(manifest) = &app.manifests[n] {
                    for region in &manifest.regions {
                        if let Some(psram_dst) = region.psram_dst {
                            let psram_ptr = PSRAM_BASE as *mut u32;
                            let spiflash_ptr = SPIFLASH_BASE as *mut u32;
                            let spiflash_offset_words = region.spiflash_src as isize / 4isize;
                            let psram_offset_words = psram_dst as isize / 4isize;
                            let size_words = region.size as isize / 4isize + 1;
                            info!("Copying {:#x}..{:#x} (spi flash) to {:#x}..{:#x} (psram) ...",
                                  SPIFLASH_BASE + region.spiflash_src as usize,
                                  SPIFLASH_BASE + (region.spiflash_src + region.size) as usize,
                                  PSRAM_BASE + psram_dst as usize,
                                  PSRAM_BASE + (psram_dst + region.size) as usize);
                            for i in 0..size_words {
                                unsafe {
                                    let d = spiflash_ptr.offset(spiflash_offset_words + i).read_volatile();
                                    psram_ptr.offset(psram_offset_words + i).write_volatile(d);
                                }
                            }
                            info!("Verify {} KiB copied correctly ...", (size_words*4) / 1024);
                            let crc_bzip2 = crc::Crc::<u32>::new(&crc::CRC_32_BZIP2);
                            let mut digest = crc_bzip2.digest();
                            for i in 0..size_words {
                                unsafe {
                                    let d1 = psram_ptr.offset(psram_offset_words + i).read_volatile();
                                    if i != (size_words - 1) {
                                        digest.update(&d1.to_le_bytes());
                                    } else {
                                        digest.update(&d1.to_le_bytes()[0..(region.size as usize)%4usize]);
                                    }
                                }
                            }
                            let crc_result = digest.finalize();
                            info!("got PSRAM crc: {:#x}, manifest wants: {:#x}", crc_result, region.crc);
                            if crc_result == region.crc {
                                info!("CRC OK.");
                            } else {
                                info!("CRC NOT OK.");
                                bad_crc = true;
                            }
                        }
                    }
                }
                if !bad_crc {
                    info!("BITSTREAM{}\n\r", n);
                    loop {}
                } else {
                    // Bad CRC: cancel reboot, draw an error.
                    app.ui.opts.toggle_modify();
                    app.reboot_n = None;
                    app.time_since_reboot_requested = 0;
                    app.error_n[n] = Some(String::from_str("ERR: CRC FAIL").unwrap());
                }
            }
        }
    });
}

pub fn write_palette(video: &mut Video0, p: palette::ColorPalette) {
    for i in 0..PX_INTENSITY_MAX {
        for h in 0..PX_HUE_MAX {
            let rgb = palette::compute_color(i, h, p);
            video.set_palette_rgb(i as u8, h as u8, rgb.r, rgb.g, rgb.b);
        }
    }
}

#[entry]
fn main() -> ! {
    let peripherals = pac::Peripherals::take().unwrap();
    let pmod = peripherals.PMOD0_PERIPH;

    let sysclk = pac::clock::sysclk();
    let serial = Serial0::new(peripherals.UART0);
    let mut timer = Timer0::new(peripherals.TIMER0, sysclk);
    let mut video = Video0::new(peripherals.VIDEO_PERIPH);

    crate::handlers::logger_init(serial);

    info!("Hello from Tiliqua bootloader!");

    let mut manifests: [Option<manifest::BitstreamManifest>; 8] = [const { None }; 8];
    for n in 0usize..N_MANIFESTS {
        let size: usize = 1024usize;
        let manifest_first = SPIFLASH_BASE + 0x100000usize;
        let addr: usize = manifest_first + (n+1)*0x100000usize - size;
        info!("(entry {}) look for manifest from {:#x} to {:#x}", n, addr, addr+size);
        manifests[n] = manifest::BitstreamManifest::from_addr(addr, size);
    }

    let opts = opts::Options::new(&manifests);
    let app = Mutex::new(RefCell::new(App::new(opts, manifests.clone())));

    handler!(timer0 = || timer0_handler(&app));

    irq::scope(|s| {

        s.register(handlers::Interrupt::TIMER0, timer0);

        timer.enable_tick_isr(TIMER0_ISR_PERIOD_MS,
                              pac::Interrupt::TIMER0);

        let mut logo_coord_ix = 0u32;
        let mut rng = fastrand::Rng::with_seed(0);
        let mut display = DMADisplay {
            framebuffer_base: PSRAM_FB_BASE as *mut u32,
        };
        video.set_persist(1024);

        let stroke = PrimitiveStyleBuilder::new()
            .stroke_color(Gray8::new(0xB0))
            .stroke_width(1)
            .build();

        write_palette(&mut video, ColorPalette::Linear);

        loop {

            let (opts, reboot_n, error_n) = critical_section::with(|cs| {
                (app.borrow_ref(cs).ui.opts.clone(),
                 app.borrow_ref(cs).reboot_n.clone(),
                 app.borrow_ref(cs).error_n.clone())
            });

            draw::draw_options(&mut display, &opts, 100, V_ACTIVE/2-50, 0).ok();
            draw::draw_name(&mut display, H_ACTIVE/2, V_ACTIVE-50, 0, UI_NAME, UI_SHA).ok();

            if let Some(n) = opts.boot.selected {
                if let Some(manifest) = &manifests[n] {
                    draw_summary(&mut display, &manifest, -20, -18, 0);
                    Line::new(Point::new(255, (V_ACTIVE/2 - 55 + (n as u32)*18) as i32),
                              Point::new((H_ACTIVE/2-90) as i32, (V_ACTIVE/2+8) as i32))
                              .into_styled(stroke)
                              .draw(&mut display).ok();
                }
                if let Some(error) = &error_n[n] {
                    let norm = MonoTextStyle::new(&FONT_9X15, Gray8::new(0xF0));
                    Text::with_alignment(
                        &error,
                        Point::new((H_ACTIVE/2) as i32 - 20, (V_ACTIVE/2) as i32 - 20),
                        norm,
                        Alignment::Left,
                    )
                    .draw(&mut display).ok();
                }
            }

            for _ in 0..5 {
                let _ = draw::draw_boot_logo(&mut display,
                                             (H_ACTIVE/2) as i32,
                                             150 as i32,
                                             logo_coord_ix);
                logo_coord_ix += 1;
            }

            if let Some(_) = reboot_n {
                pmod.flags().write(|w|  w.mute().bit(true) );
                print_rebooting(&mut display, &mut rng);
            }
        }
    })
}
