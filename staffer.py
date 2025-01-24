

import logging
import pickle
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union, cast

import cv2
import matplotlib.pyplot as plt
import numpy as np
from cv2.typing import MatLike
from pdf2image import convert_from_path
from scipy.signal import find_peaks


class KernReader:

    lines: List[str]
    bars: Dict[int, int] = {}

    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self.load_tokens()

    BAR_RE = re.compile(r'^=+\s*(\d+)?.*$')

    def load_tokens(self):
        with open(self.path.with_suffix(".tokens"), "r") as fp:
            self.lines = [line.strip() for line in fp.readlines()]
        # Constructs the bars index.
        for lineno in range(0, len(self.lines)):
            line = self.lines[lineno]
            if (m := self.BAR_RE.match(line)):
                self.bars[int(m.group(1))] = lineno
        logging.info(f"{len(self.bars)} bars in {self.path}")

    def get_text(self, barno: int, count: int = 10) -> Optional[List[str]]:
        pos = self.bars.get(barno, -1)
        if pos >= 0:
            return self.lines[pos:pos+count]
        else:
            return None


class Staffer:
    """Finds the layout of a score.

        Given a pdf file, Staffer converts each page to a normalized
        image, ready for training a model, and provides the corrdinates
        of staves on each page of the score.
    """
    WIDTH = 1200

    @dataclass
    class Staff:
        rh_top: int
        lh_bot: int
        bars: List[int]

    @dataclass
    class Page:

        # Page number in the pdf (countint from 0)
        pageno: int

        # For each double staff (right hand, left hand)
        # (rh_top: top of rh, lh_bot: bottom of lh)
        staves: List['Staffer.Staff']

        # Manually reviewed? Turns on when saved through the editor.
        reviewed: bool = False

    do_plot: bool
    no_cache: bool
    width: int
    data: Optional[List[Tuple[MatLike, Page]]] = None

    def __init__(
        self, pdf_path: Path,
        width: int = WIDTH, do_plot: bool = False, no_cache: bool = False
    ):
        self.pdf_path = pdf_path
        self.width = width
        self.do_plot = do_plot
        self.no_cache = no_cache

    def denoise(self, image: MatLike, block_size: int = -1, C: int = 2) -> MatLike:
        if block_size > 0:
            return cv2.adaptiveThreshold(
                image,
                maxValue=255,
                adaptiveMethod=cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                thresholdType=cv2.THRESH_BINARY,
                blockSize=block_size,
                C=C
            )
        else:
            _, image = cv2.threshold(
                image,
                127, 255,
                cv2.THRESH_OTSU
            )
            return image

    def average_angle(self, image: MatLike, hough_thresold: int = 500) -> float:
        edges = cv2.Canny(image, 50, 150, apertureSize=3)
        hough_lines = cv2.HoughLines(
            edges, 1, np.pi / 14400, hough_thresold,
            min_theta=np.radians(80), max_theta=np.radians(100)
        )
        hough_lines = hough_lines.squeeze(1)
        # Computes the median average angle:
        median_angle = np.median(hough_lines[:, 1])  # type: ignore
        return np.degrees(median_angle) - 90.0

    def deskew(
        self,
        image: MatLike,
        min_rotation_angle_degrees=0.05,
    ):
        angle = self.average_angle(image)
        if angle < min_rotation_angle_degrees:
            # For a 1200px width, 600px center to side:
            # height = 600 * sin(0.1 degrees) ~ 1 px
            return image
        height, width = image.shape
        matrix = cv2.getRotationMatrix2D((width // 2, height//2), angle, 1)
        image = cv2.warpAffine(image, matrix, (width, height))
        print(f"\t{angle:2.4f} rotation => {
              self.average_angle(image):2.4f} degrees.")
        return image

    def transform(self, orig_image: MatLike) -> MatLike:
        image = cv2.cvtColor(np.array(orig_image), cv2.COLOR_RGB2GRAY)
        image = self.denoise(image)
        image = self.deskew(image)
        # Some of the operations above de-binarize the images.
        image = self.denoise(image)
        return image

    def decode_images(self) -> List[MatLike]:
        # Runs the pdf conversion with all transformations.
        pdf = convert_from_path(self.pdf_path.with_suffix(".pdf"))
        # Rescale to requested width and pad to requested height.

        def resize(image) -> MatLike:
            h, w = image.shape[:-1]
            scale = self.width / w
            image = cv2.resize(
                image, (self.width, int(h * scale)), interpolation=cv2.INTER_AREA
            )
            return image

        return [resize(np.array(image)) for image in pdf]

    def line_indices(self, lines: List[int]) -> List[Tuple[int, int]]:
        idx = 0
        out = []
        while idx+9 < len(lines):
            out.append((idx, idx+9))
            idx += 10
        return out

    def find_bars(
            self, image: MatLike, fill_rate: float = 0.8
    ) -> List[int]:
        x_proj = np.sum(image, 0)
        # A bar goes from top to bottom, so we can compute a min height for
        # our peak by assuming a percentage fill rate on that vertical line.
        peak_height = int(image.shape[0] * fill_rate * 255)
        bar_peaks, properties = find_peaks(
            x_proj, distance=50,  height=peak_height)
        bar_peaks = [p.item() for p in bar_peaks]
        # bar_heights = [h.item() for h in properties['peak_heights']]
        # print(list(zip(bar_peaks, bar_heights)))

        if self.do_plot:
            plt.subplot()
            plt.vlines(bar_peaks, ymin=0, ymax=50000, colors='r')
            plt.plot(x_proj)
            plt.show()

        return bar_peaks

    def decode_page(self, orig_image: MatLike, pageno: int) -> Page:
        # Computes the vertical and horizontal projections.
        image = cv2.bitwise_not(orig_image)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 1))
        image = cv2.morphologyEx(image, cv2.MORPH_CLOSE, kernel)
        y_lines = np.sum(image, 1)

        # Computes the moving average of the y projection and gets the peaks
        # of this series to obtain the rough position of each staff.
        wsize = 50
        y_proj = np.zeros(y_lines.shape)
        offset = (wsize - 1) // 2
        y_avg = np.convolve(y_lines, np.ones(wsize) / wsize, mode='valid')
        y_proj[offset:offset+len(y_avg)] = y_avg
        # The width=20 is the max allowed for chopin/mazurka/mazurka30-3
        # Height of 45_000 is for mozart/piano/sonata/sonata01-1
        staff_peaks, _ = find_peaks(
            y_proj, width=20, distance=50, height=45_000)

        # Around each staff peak, find the peaks corresponding to each staff line.
        staff_lines = []
        for peak in staff_peaks:
            peak_height = 75000
            staff_range = y_lines[peak-40:peak+40]
            line_peaks, properties = find_peaks(
                staff_range, height=peak_height, distance=7)
            if len(line_peaks) > 5:
                # We're pretty lax on our find_peaks() param, in case we
                # get too many peaks, we throw the smallest ones out.
                sorted_peaks = sorted(
                    zip(line_peaks, properties['peak_heights'].tolist()),
                    key=lambda x: x[1],
                    reverse=True
                )
                line_peaks = [line_peak for line_peak, _ in sorted_peaks]
                line_peaks = line_peaks[:5]
            elif len(line_peaks) < 5:
                # Lowers the thresold until we get 5 lines.
                while peak_height > 0 and len(line_peaks) < 5:
                    peak_height -= 2500
                    line_peaks, properties = find_peaks(
                        staff_range, height=peak_height, distance=7)
            staff_lines.extend([p+peak-40 for p in line_peaks])

        staff_lines = sorted(staff_lines)

        if self.do_plot:
            plt.subplot()
            plt.plot(y_lines, 'b', label='y_lines')
            plt.plot(y_proj, 'b', label='y_lines', linestyle="dotted")
            plt.vlines(staff_peaks, ymin=0, ymax=300000, colors='r')
            plt.vlines(staff_lines, ymin=0, ymax=100000, colors='g')
            plt.show()

        if len(staff_lines) % 10 != 0:
            # Trims off excess lines
            length_10 = 10 * (len(staff_lines) // 10)
            staff_lines = staff_lines[:length_10]
            print(f"Trimming off {len(staff_lines) -
                  length_10} bottom staff lines.")

        page = Staffer.Page(pageno, staves=[])
        positions = [
            (staff_lines[ridx], staff_lines[lidx]) for ridx, lidx in self.line_indices(staff_lines)
        ]

        # Left and right are top and last offsets of vertical lines.
        image = cv2.bitwise_not(orig_image)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 5))
        image = cv2.morphologyEx(image, cv2.MORPH_CLOSE, kernel)
        for rh_top, lh_bot in positions:
            bars = self.find_bars(image[rh_top:lh_bot, :])
            page.staves.append(Staffer.Staff(rh_top, lh_bot, bars))

        return page

    def load_if_exists(self) -> bool:
        if self.no_cache:
            return False
        pkl_path = self.pdf_path.with_suffix(".pkl")
        if pkl_path.exists():
            with open(pkl_path, "rb") as fp:
                self.data = cast(
                    List[Tuple[MatLike, Staffer.Page]], pickle.load(fp))
                return True
        return False

    def save(self):
        pkl_path = self.pdf_path.with_suffix(".pkl")
        with open(pkl_path, "wb+") as fp:
            pickle.dump(self.data, fp)

    def staff(self) -> List[Tuple[MatLike, Page]]:
        if self.data is None:
            if not self.load_if_exists():
                images = self.decode_images()
                staves = [self.decode_page(self.transform(image), pageno)
                          for pageno, image in enumerate(images)]
                self.data = list(zip(images, staves))
                self.save()
        assert self.data is not None, f"{self.pdf_path}: no staff found."
        return self.data

    def draw_page(
        self, image: MatLike, page: Page, bar_offset: int,
        selected_staff: int = -1,
        selected_bar: int = -1,
        thickness: int = 2
    ) -> MatLike:
        BLUE = (255, 0, 0)
        RED = (0, 0, 255)
        GREEN = (0, 255, 0)
        style, selected_style = (BLUE, 2), (RED, 4)
        if page.reviewed:
            style = (GREEN, 2)
        rgb_image = image.copy()
        for staffno, staff in enumerate(page.staves):
            # Draws the bars.
            for barno, bar in enumerate(staff.bars):
                color, thickness = selected_style if (staffno == selected_staff) and (
                    barno == selected_bar) else style
                cv2.line(
                    rgb_image,
                    (bar, staff.rh_top),
                    (bar, staff.lh_bot),
                    color, thickness
                )
                # Renders the bar number only if not last of staff.
                if barno != len(staff.bars) - 1:
                    cv2.putText(
                        rgb_image, str(bar_offset + barno + 1),
                        (bar+3, staff.rh_top - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, fontScale=1,
                        color=color, thickness=thickness)
            bar_offset += len(staff.bars) - 1
            # Draws the top most and bottom most staff lines:
            color, thickness = selected_style if (
                staffno == selected_staff) else style
            cv2.line(
                rgb_image,
                (staff.bars[0], staff.rh_top),
                (staff.bars[-1], staff.rh_top),
                color, thickness
            )
            # Left hand staff
            cv2.line(
                rgb_image,
                (staff.bars[0], staff.lh_bot),
                (staff.bars[-1], staff.lh_bot),
                color,
                thickness
            )
        return rgb_image

    def edit(self):
        position = 0
        selected_staff, selected_bar = 0, 0
        max_height = 1024
        data = self.staff()
        kern = KernReader(self.pdf_path)

        def bars_to(pageno: int) -> int:
            barno = 0
            for _, page in data[:pageno]:
                for staff in page.staves:
                    barno += len(staff.bars) - 1
            return barno

        while True:

            image, page = data[position]
            bars_offset = bars_to(position)
            image = self.draw_page(
                image, page, bars_offset, selected_staff, selected_bar
            )
            height, width = image.shape[:2]
            if max_height > 0 and height > max_height:
                new_width = int(max_height * width / height)
                image = cv2.resize(image, (new_width, max_height))

            cv2.imshow("edit", image)
            key = cv2.waitKey()
            if key == ord('q'):
                return
            elif key == ord('s'):
                # Marks all pages as reviewed before saving.
                assert self.data is not None, "Makes the type checker happy."
                self.save()
                print(f"{len(self.data)} pages reviewed and saved to {
                      self.pdf_path.with_suffix('.pkl')}.")
            elif key == ord('n') or key == ord(' '):
                position = (position + 1) % len(data)
                selected_staff, selected_bar = 0, 0
            elif key == ord('p'):
                position = max(0, position - 1)
                selected_staff, selected_bar = 0, 0
            elif key == ord('j'):    # Moves selected bar left.
                page.staves[selected_staff].bars[selected_bar] -= 5
            elif key == ord('J'):    # Moves selected bar left slow.
                page.staves[selected_staff].bars[selected_bar] -= 1
            elif key == ord('k'):
                # Show kerns matching this bar.
                barno = bars_offset
                for i in range(0, selected_staff):
                    barno += len(page.staves[i].bars) - 1
                barno += selected_bar
                # Displays the kern tokens:
                records = kern.get_text(barno + 1)
                if records is None:
                    print(f"No records found for bar {barno + 1}")
                else:
                    print(f"Bar {barno + 1}:")
                    for record in records:
                        print(record)
            elif key == ord('l'):    # Moves selected bar right.
                page.staves[selected_staff].bars[selected_bar] += 5
            elif key == ord('L'):    # Moves selected bar right slow.
                page.staves[selected_staff].bars[selected_bar] += 1
            elif key == ord('m'):
                page.reviewed = True
            elif key == 84:     # Key up
                if selected_staff + 1 >= len(page.staves):
                    if position + 1 >= len(data):
                        print("End of score.")
                    else:
                        position += 1
                        selected_staff, selected_bar = 0, 0
                else:
                    selected_staff, selected_bar = selected_staff + 1, 0
            elif key == 82:     # Key down
                if selected_staff - 1 < 0:
                    if position - 1 < 0:
                        print("Beginning of score.")
                    else:
                        position -= 1
                        selected_staff, selected_bar = (
                            len(data[position][1].staves) - 1,
                            0
                        )
                else:
                    selected_staff, selected_bar = selected_staff - 1, 0
            elif key == 83:     # Key left
                selected_bar = (
                    selected_bar + 1) % len(page.staves[selected_staff].bars)
            elif key == 81:     # Key right
                selected_bar = max(0, selected_bar - 1)
            elif key == ord('h') or key == ord('?'):
                print("""
'q'     Quit editor without saving.
'n'     Next page of current score.
'p'     Previous page of current score.
'l'/'L' Move selected bar left, fast & slow.
'j'/'J' Move selected bar right, fast & slow.
'm'     Toggle on/off current page as reviewed.
Up      Select staff above.
Down    Select staff below.
Right   Select staff below.
Left    Select staff below.
'h','?' This help.
                      """)
            else:
                print(f"Key: {key}")


if __name__ == '__main__':
    staffer = Staffer(
        Path("/home/anselm/datasets/kern-sheet/bach/inventions/inven01"),
        no_cache=True
    )
    staffer.edit()
