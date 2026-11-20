from io import BytesIO

import qrcode
from django.conf import settings
from django.core import signing
from django.urls import reverse
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    Image,
)


TICKET_VERIFY_SALT = "bookmyseat.ticket.verify"


def make_verify_token(booking_id):
    """Signed token embedded in the QR code. Not guessable, so booking IDs cannot be enumerated."""
    return signing.dumps(booking_id, salt=TICKET_VERIFY_SALT)


def is_valid_verify_token(token, booking_id):
    if not token:
        return False
    try:
        return signing.loads(token, salt=TICKET_VERIFY_SALT) == booking_id
    except signing.BadSignature:
        return False


def generate_ticket_pdf(payment, bookings):
    """
    Generate one PDF ticket for a successful payment.

    The ticket contains:
    - Movie
    - Theater
    - Screen
    - Show timing
    - Seats
    - Booking IDs
    - Payment reference
    - Amount
    - QR code for ticket verification
    """

    bookings = list(bookings)

    if not bookings:
        raise ValueError("No bookings found for this payment.")

    first_booking = bookings[0]

    movie = first_booking.movie
    theater = first_booking.theater

    buffer = BytesIO()

    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "TicketTitle",
        parent=styles["Title"],
        fontSize=24,
        leading=28,
        alignment=1,
        spaceAfter=12,
    )

    heading_style = ParagraphStyle(
        "TicketHeading",
        parent=styles["Heading2"],
        fontSize=14,
        leading=18,
        spaceAfter=8,
    )

    normal_style = ParagraphStyle(
        "TicketNormal",
        parent=styles["Normal"],
        fontSize=10,
        leading=14,
    )

    story = []

    # --------------------------------------------------------
    # HEADER
    # --------------------------------------------------------

    story.append(
        Paragraph("BOOKMYSEAT", title_style)
    )

    story.append(
        Paragraph("MOVIE TICKET", heading_style)
    )

    story.append(
        Spacer(1, 5 * mm)
    )

    # --------------------------------------------------------
    # QR CODE
    # --------------------------------------------------------

    verification_url = (
        settings.SITE_BASE_URL
        + reverse(
            "verify_ticket",
            kwargs={"booking_id": first_booking.id},
        )
        + "?t="
        + make_verify_token(first_booking.id)
    )

    qr = qrcode.make(verification_url)

    qr_buffer = BytesIO()
    qr.save(qr_buffer, format="PNG")
    qr_buffer.seek(0)

    qr_image = Image(
        qr_buffer,
        width=35 * mm,
        height=35 * mm,
    )

    # --------------------------------------------------------
    # TICKET DETAILS
    # --------------------------------------------------------

    
    def cell_text(value):
        """Create a wrapping PDF cell for long text."""
        if value is None or value == "":
            value = "N/A"

        return Paragraph(
            str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"),
            normal_style,
        )

    details = [
        ["Movie", cell_text(movie.name)],
        ["Theater", cell_text(theater.name)],
        ["Screen", cell_text(theater.screen)],
        [
            "Show Time",
            cell_text(timezone.localtime(theater.time).strftime("%d %B %Y, %I:%M %p")),
        ],
        [
            "Seats",
            cell_text(", ".join(
                str(booking.seat.seat_number) for booking in bookings
            )),
        ],
        [
            "Booking ID(s)",
            cell_text(", ".join(str(booking.id) for booking in bookings)),
        ],
        [
            "Payment Reference",
            cell_text(
                payment.razorpay_payment_id or payment.razorpay_order_id or "N/A"
            ),
        ],
        ["Amount", cell_text(f"INR {payment.amount}")],
    ]

    table = Table(
        details,
        colWidths=[45 * mm, 105 * mm],
        repeatRows=0,
    )

    table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eeeeee")),
            ("TEXTCOLOR", (0, 0), (0, -1), colors.black),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ])
    )

    story.append(table)
    story.append(Spacer(1, 10 * mm))

    # --------------------------------------------------------
    # QR CODE SECTION
    # --------------------------------------------------------

    qr_table = Table(
        [
            [
                Paragraph(
                    "<b>Scan to verify ticket</b><br/>"
                    "Present this QR code at the theater.",
                    normal_style,
                ),
                qr_image,
            ]
        ],
        colWidths=[100 * mm, 45 * mm],
    )

    qr_table.setStyle(
        TableStyle(
            [
                (
                    "VALIGN",
                    (0, 0),
                    (-1, -1),
                    "MIDDLE",
                ),
                (
                    "ALIGN",
                    (1, 0),
                    (1, 0),
                    "CENTER",
                ),
            ]
        )
    )

    story.append(qr_table)

    story.append(
        Spacer(1, 10 * mm)
    )

    # --------------------------------------------------------
    # FOOTER
    # --------------------------------------------------------

    story.append(
        Paragraph(
            "Thank you for booking with BookMySeat.",
            normal_style,
        )
    )

    story.append(
        Paragraph(
            "This ticket is generated electronically and "
            "can be verified using the QR code.",
            normal_style,
        )
    )

    # --------------------------------------------------------
    # BUILD PDF
    # --------------------------------------------------------

    document.build(story)

    buffer.seek(0)

    return buffer.getvalue()