import "bootstrap/dist/css/bootstrap.min.css";
import "bootstrap-select/dist/css/bootstrap-select.min.css";
import "@fortawesome/fontawesome-free/css/all.min.css";
import "../css/style.scss";

import "bootstrap";
import "bootstrap-select";
import "jquery";
import "htmx.org";

$(() => {
    document.body.addEventListener("htmx:afterSwap", function () {
        console.log($);
        console.log($('.selectpicker'));
        console.log($('.selectpicker').selectpicker);
        $('.selectpicker').selectpicker();
    });
})

  